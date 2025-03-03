import instaloader
import sqlite3
import time
import random
import logging
import json
import threading
from datetime import datetime
from typing import Dict, List, Optional
from langdetect import detect, LangDetectException
import openai

# НАСТРОЙКА ЛОГГИРОВАНИЯ

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_activity.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# КЛАСС КОНФИГУРАЦИИ

class ConfigLoader:
    """Загрузка и валидация конфигурации из JSON файла"""
    
    REQUIRED_FIELDS = ['openai_api_keys', 'accounts']
    
    def __init__(self, config_path: str = 'insta_config.json'):
        self.config_path = config_path
        self.config = self._load_and_validate()

    def _load_and_validate(self) -> Dict:
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
                
            for field in self.REQUIRED_FIELDS:
                if field not in config:
                    raise ValueError(f"Отсутствует обязательное поле: {field}")
                    
            return config
            
        except FileNotFoundError:
            logger.critical(f"Файл конфигурации {self.config_path} не найден")
            exit(1)
        except json.JSONDecodeError:
            logger.critical("Ошибка формата JSON в конфигурационном файле")
            exit(1)

    @property
    def openai_keys(self) -> List[str]:
        return self.config['openai_api_keys']
    
    @property
    def accounts(self) -> Dict[str, Dict]:
        return self.config['accounts']
    
    @property
    def db_name(self) -> str:
        return self.config.get('db_name', 'instagram_bot.db')
    
    @property
    def request_delay(self) -> List[int]:
        return self.config.get('request_delay', [30, 60])
    
    @property
    def check_interval(self) -> int:
        return self.config.get('check_interval_hours', 4)

# ИИ-ГЕНЕРАТОР ОТВЕТОВ

class AIResponseGenerator:
    """Генерация ответов с использованием OpenAI GPT"""
    
    def __init__(self, config: ConfigLoader):
        self.config = config
        self.current_key_idx = 0
        openai.api_key = self.current_key
        
    @property
    def current_key(self) -> str:
        return self.config.openai_keys[self.current_key_idx]
    
    def _rotate_key(self):
        self.current_key_idx = (self.current_key_idx + 1) % len(self.config.openai_keys)
        openai.api_key = self.current_key
        logger.info(f"Переключено на OpenAI ключ #{self.current_key_idx + 1}")

    def generate_response(self, text: str, personality: str, lang_map: Dict) -> Optional[str]:
        """Генерация ответа на комментарий"""
        try:
            lang = self._detect_language(text)
            system_prompt = self._build_prompt(personality, lang_map, lang)
            
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Комментарий: {text}"}
                ],
                temperature=0.7,
                max_tokens=150
            )
            return response.choices[0].message['content'].strip()
            
        except openai.error.RateLimitError:
            self._rotate_key()
            return None
        except Exception as e:
            logger.error(f"Ошибка ИИ: {str(e)}")
            return None

    @staticmethod
    def _detect_language(text: str) -> str:
        try:
            return detect(text)
        except LangDetectException:
            return 'en'

    @staticmethod
    def _build_prompt(personality: str, lang_map: Dict, lang: str) -> str:
        target_lang = lang_map.get(lang, 'English')
        return f"""
        Ты {personality}. Правила ответа:
        1. Отвечай на {target_lang}
        2. Будь дружелюбным и профессиональным
        3. Используй не более 2 предложений
        4. Не давай финансовых советов
        5. Избегай сленга и аббревиатур
        6. Добавь 1-2 эмодзи по теме
        """

# БАЗА ДАННЫХ

class DatabaseManager:
    """Управление SQLite базой данных"""
    
    def __init__(self, db_name: str):
        self.db_name = db_name
        self._init_db()
        
    def _init_db(self):
        """Инициализация таблиц"""
        with self.connection as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS comments (
                    account_id TEXT,
                    comment_id TEXT,
                    post_id TEXT,
                    text TEXT,
                    is_reply INTEGER,
                    response TEXT,
                    timestamp DATETIME,
                    PRIMARY KEY(account_id, comment_id)
                )
            ''')
            conn.commit()

    @property
    def connection(self):
        return sqlite3.connect(self.db_name)

# ИНСТАГРАМ БОТ

class InstagramBot:
    """Обработчик одного Instagram аккаунта"""
    
    def __init__(self, account_id: str, config: ConfigLoader, db: DatabaseManager, ai: AIResponseGenerator):
        self.account_id = account_id
        self.config = config
        self.db = db
        self.ai = ai
        self.loader = self._setup_instaloader()
        self._login()
        
    def _setup_instaloader(self) -> instaloader.Instaloader:
        """Настройка Instaloader с прокси"""
        L = instaloader.Instaloader(
            user_agent="Mozilla/5.0 (X11; Linux x86_64)",
            request_timeout=120,
            max_connection_attempts=3
        )
        
        if 'proxy' in self.config.accounts[self.account_id]:
            proxy = self.config.accounts[self.account_id]['proxy']
            L.context._session.proxies = {'http': proxy, 'https': proxy}
            logger.info(f"Используется прокси для {self.account_id}")
            
        return L
    
    def _login(self):
        """Авторизация в Instagram"""
        try:
            acc_cfg = self.config.accounts[self.account_id]
            self.loader.load_session_from_file(acc_cfg['ig_username'])
        except FileNotFoundError:
            self.loader.login(acc_cfg['ig_username'], acc_cfg['ig_password'])
            self.loader.save_session_to_file()
            logger.info(f"Успешная авторизация: {self.account_id}")
    
    def run(self):
        """Основной цикл работы"""
        while True:
            try:
                self._process_new_posts()
                self._reply_to_comments()
                time.sleep(self.config.check_interval * 3600)
            except Exception as e:
                logger.error(f"Ошибка в аккаунте {self.account_id}: {str(e)}")
                time.sleep(600)
    
    def _process_new_posts(self):
        """Обработка новых постов целевого аккаунта"""
        target_account = self.config.accounts[self.account_id]['target_account']
        try:
            profile = instaloader.Profile.from_username(self.loader.context, target_account)
            for post in profile.get_posts():
                if self._is_post_processed(post.shortcode):
                    continue
                self._save_comments(post)
        except instaloader.exceptions.TooManyRequestsException:
            logger.warning("Слишком много запросов! Пауза 1 час.")
            time.sleep(3600)
            self._process_new_posts()
    
    def _is_post_processed(self, post_id: str) -> bool:
        """Проверка обработки поста"""
        with self.db.connection as conn:
            cursor = conn.execute(
                "SELECT 1 FROM comments WHERE post_id = ? AND account_id = ? LIMIT 1",
                (post_id, self.account_id)
            )
            return cursor.fetchone() is not None
    
    def _save_comments(self, post: instaloader.Post):
        """Сохранение комментариев поста"""
        try:
            comments = post.get_comments()
            for idx, comment in enumerate(comments):
                if idx >= 20 or comment.answer_to is not None:
                    continue
                
                with self.db.connection as conn:
                    conn.execute('''
                        INSERT OR IGNORE INTO comments 
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        self.account_id,
                        comment.id,
                        post.shortcode,
                        comment.text,
                        0,  # is_reply
                        None,
                        datetime.now()
                    ))
                    conn.commit()
                    
                time.sleep(random.uniform(1, 3))
                
        except Exception as e:
            logger.error(f"Ошибка сохранения комментариев: {str(e)}")
    
    def _reply_to_comments(self):
        """Отправка ответов на комментарии"""
        with self.db.connection as conn:
            cursor = conn.execute('''
                SELECT * FROM comments 
                WHERE 
                    account_id = ? AND
                    response IS NULL AND
                    is_reply = 0
                LIMIT ?
            ''', (self.account_id, self.config.accounts[self.account_id].get('max_replies', 15)))
            
            for comment in cursor.fetchall():
                self._send_ai_reply(comment)
                time.sleep(random.randint(*self.config.request_delay))
    
    def _send_ai_reply(self, comment_data: Dict):
        """Отправка AI-ответа"""
        try:
            ai_response = self.ai.generate_response(
                comment_data['text'],
                self.config.accounts[self.account_id]['personality'],
                self.config.accounts[self.account_id]['lang_map']
            )
            
            if ai_response:
                post = instaloader.Post.from_shortcode(self.loader.context, comment_data['post_id'])
                post.add_comment(ai_response)
                
                with self.db.connection as conn:
                    conn.execute('''
                        UPDATE comments 
                        SET response = ?
                        WHERE comment_id = ? AND account_id = ?
                    ''', (ai_response, comment_data['comment_id'], self.account_id))
                    conn.commit()
                    
                logger.info(f"Ответ отправлен: {comment_data['comment_id'][:10]}...")
        
        except instaloader.exceptions.BadResponseException as e:
            logger.error(f"Ошибка Instagram API: {str(e)}")
            time.sleep(300)

# МЕНЕДЖЕР БОТОВ


class BotManager:
    """Управление пулом Instagram ботов"""
    
    def __init__(self, config: ConfigLoader):
        self.config = config
        self.db = DatabaseManager(config.db_name)
        self.ai = AIResponseGenerator(config)
        self.bots = self._init_bots()
    
    def _init_bots(self) -> Dict[str, InstagramBot]:
        return {
            acc_id: InstagramBot(acc_id, self.config, self.db, self.ai)
            for acc_id in self.config.accounts.keys()
        }
    
    def start(self):
        """Запуск ботов в отдельных потоках"""
        for acc_id, bot in self.bots.items():
            thread = threading.Thread(
                target=bot.run,
                name=f"Bot-{acc_id}",
                daemon=True
            )
            thread.start()
            logger.info(f"Запущен бот для аккаунта: {acc_id}")
        
        while True:
            time.sleep(3600)
            self._check_threads()
    
    def _check_threads(self):
        """Мониторинг активности"""

if __name__ == '__main__':
    config = ConfigLoader()
    manager = BotManager(config)
    manager.start()
