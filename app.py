"""
WebSocket прокси сервер для Google Gemini Live API
Проксирует WebSocket соединения от клиента к Google API через HTTP прокси
Развертывается на Render как отдельный сервис

ВАЖНО: Использует Flask-SocketIO для работы WebSocket на том же порту что и Flask
Это необходимо для Render, который предоставляет только один порт
"""

import os
import asyncio
import websockets
import json
import logging
import threading
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import eventlet

# Патчим eventlet для поддержки asyncio
eventlet.monkey_patch()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# URL WebSocket для Google Gemini Live API
GEMINI_WS_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService/BidiGenerateContent"

# Инициализация Flask приложения
app = Flask(__name__)
CORS(app)  # Разрешаем CORS для всех доменов

# Инициализация SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', logger=True, engineio_logger=True)

# Хранилище активных WebSocket соединений к Google
google_connections = {}
client_api_keys = {}  # Хранилище API ключей для каждого клиента

def get_proxy_config():
    """Получает конфигурацию прокси из переменных окружения"""
    proxy_url = os.getenv('HTTP_PROXY') or os.getenv('HTTPS_PROXY') or os.getenv('PROXY_URL') or os.getenv('PROXY')
    
    if not proxy_url:
        return None
    
    try:
        parsed = urlparse(proxy_url)
        return {
            'host': parsed.hostname,
            'port': int(parsed.port) if parsed.port else 80,
            'username': parsed.username,
            'password': parsed.password,
            'url': proxy_url,
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга прокси URL: {e}")
        return None

def create_google_connection(client_id: str, api_key: str):
    """
    Создает соединение к Google API через HTTP прокси
    Запускается в отдельном потоке для каждого клиента
    """
    try:
        proxy_config = get_proxy_config()
        headers = {"x-goog-api-key": api_key}
        google_ws_url = f"{GEMINI_WS_URL}?key={api_key}"
        
        # Устанавливаем прокси если есть
        original_http_proxy = os.environ.get('HTTP_PROXY')
        original_https_proxy = os.environ.get('HTTPS_PROXY')
        
        if proxy_config:
            os.environ['HTTP_PROXY'] = proxy_config['url']
            os.environ['HTTPS_PROXY'] = proxy_config['url']
            logger.info(f"Подключение через прокси {proxy_config['host']}:{proxy_config['port']}")
        
        try:
            # Создаем event loop в новом потоке
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # Создаем соединение к Google
            async def connect_and_forward():
                try:
                    # ПРОБЛЕМА: websockets.connect() с extra_headers не работает с eventlet/asyncio
                    # РЕШЕНИЕ: API ключ уже передается через query параметр ?key=api_key в URL
                    # Дополнительный заголовок x-goog-api-key не нужен, так как ключ в URL
                    # Если Google API требует заголовок, используем подкласс WebSocketClientProtocol
                    
                    try:
                        # Пробуем подключиться без extra_headers (ключ уже в URL)
                        google_ws = await websockets.connect(google_ws_url)
                    except Exception as e1:
                        logger.warning(f"Ошибка подключения без заголовков: {e1}, пробую с подклассом")
                        # Если не работает, пробуем создать кастомный протокол
                        from websockets.client import WebSocketClientProtocol
                        
                        class HeaderWebSocketProtocol(WebSocketClientProtocol):
                            """WebSocket протокол с поддержкой дополнительных заголовков"""
                            def __init__(self, *args, custom_headers=None, **kwargs):
                                self.custom_headers = custom_headers or {}
                                super().__init__(*args, **kwargs)
                            
                            async def handshake(self, *args, **kwargs):
                                # Добавляем заголовки во время handshake
                                if self.custom_headers:
                                    for key, value in self.custom_headers.items():
                                        self.request_headers[key] = value
                                return await super().handshake(*args, **kwargs)
                        
                        try:
                            # Используем кастомный протокол с заголовками
                            google_ws = await websockets.connect(
                                google_ws_url,
                                create_protocol=lambda: HeaderWebSocketProtocol(custom_headers=headers)
                            )
                        except Exception as e2:
                            logger.error(f"Ошибка подключения с кастомным протоколом: {e2}")
                            raise e2
                    
                    google_connections[client_id] = google_ws
                    logger.info(f"✅ Соединение с Google API установлено для {client_id}")
                    
                    # Запускаем задачу для чтения от Google
                    async def read_from_google():
                        try:
                            async for message in google_ws:
                                # Отправляем клиенту через SocketIO
                                socketio.emit('gemini_message', {
                                    'data': message.decode('utf-8') if isinstance(message, bytes) else message,
                                    'type': 'text' if isinstance(message, str) else 'binary'
                                }, room=client_id)
                                logger.debug(f"Получено сообщение от Google для {client_id}")
                        except websockets.exceptions.ConnectionClosed:
                            logger.info(f"Соединение с Google закрыто для {client_id}")
                            if client_id in google_connections:
                                del google_connections[client_id]
                        except Exception as e:
                            logger.error(f"Ошибка при чтении от Google: {e}", exc_info=True)
                            if client_id in google_connections:
                                try:
                                    await google_connections[client_id].close()
                                except:
                                    pass
                                del google_connections[client_id]
                    
                    # Запускаем чтение
                    await read_from_google()
                    
                except Exception as e:
                    logger.error(f"Ошибка подключения к Google: {e}", exc_info=True)
                    socketio.emit('error', {'message': str(e)}, room=client_id)
                    if client_id in google_connections:
                        del google_connections[client_id]
            
            # Запускаем в event loop
            loop.run_until_complete(connect_and_forward())
            
        finally:
            # Восстанавливаем переменные окружения
            if original_http_proxy:
                os.environ['HTTP_PROXY'] = original_http_proxy
            elif 'HTTP_PROXY' in os.environ:
                del os.environ['HTTP_PROXY']
            if original_https_proxy:
                os.environ['HTTPS_PROXY'] = original_https_proxy
            elif 'HTTPS_PROXY' in os.environ:
                del os.environ['HTTPS_PROXY']
                
    except Exception as e:
        logger.error(f"Ошибка создания соединения к Google: {e}", exc_info=True)
        socketio.emit('error', {'message': str(e)}, room=client_id)

# SocketIO события
@socketio.on('connect')
def handle_connect(auth):
    """Обработчик подключения WebSocket клиента"""
    client_id = request.sid
    logger.info(f"WebSocket клиент подключился: {client_id}")
    
    # Получаем API ключ из query параметров или auth
    api_key = request.args.get('api_key') or (auth.get('api_key') if auth else None)
    
    if api_key:
        client_api_keys[client_id] = api_key
        logger.info(f"API ключ получен для {client_id}: {api_key[:10]}...")
        # Создаем соединение к Google в отдельном потоке
        thread = threading.Thread(
            target=create_google_connection,
            args=(client_id, api_key),
            daemon=True
        )
        thread.start()
    
    emit('connected', {'status': 'connected', 'client_id': client_id})

@socketio.on('disconnect')
def handle_disconnect():
    """Обработчик отключения WebSocket клиента"""
    client_id = request.sid
    logger.info(f"WebSocket клиент отключился: {client_id}")
    
    # Закрываем соединение с Google
    if client_id in google_connections:
        try:
            google_ws = google_connections[client_id]
            # Закрываем в отдельном потоке
            def close_connection():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(google_ws.close())
                loop.close()
            thread = threading.Thread(target=close_connection, daemon=True)
            thread.start()
        except:
            pass
        del google_connections[client_id]
    
    if client_id in client_api_keys:
        del client_api_keys[client_id]

@socketio.on('message')
def handle_message(data):
    """Обработчик сообщений от WebSocket клиента"""
    try:
        client_id = request.sid
        api_key = client_api_keys.get(client_id)
        
        if not api_key:
            emit('error', {'message': 'API key required. Send it in connect query or init event'}, room=client_id)
            logger.warning(f"API ключ не найден для {client_id}")
            return
        
        # Проверяем наличие соединения к Google
        if client_id not in google_connections:
            logger.warning(f"Соединение к Google не создано для {client_id}, создаю...")
            thread = threading.Thread(
                target=create_google_connection,
                args=(client_id, api_key),
                daemon=True
            )
            thread.start()
            emit('info', {'message': 'Connecting to Google...'}, room=client_id)
            return
        
        # Отправляем сообщение к Google
        def send_to_google():
            try:
                google_ws = google_connections[client_id]
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                async def send():
                    try:
                        if isinstance(data, str):
                            await google_ws.send(data)
                        elif isinstance(data, dict):
                            await google_ws.send(json.dumps(data))
                        else:
                            await google_ws.send(data)
                        logger.debug(f"Отправлено сообщение от {client_id} к Google")
                    except Exception as e:
                        logger.error(f"Ошибка отправки к Google: {e}", exc_info=True)
                        socketio.emit('error', {'message': str(e)}, room=client_id)
                
                loop.run_until_complete(send())
            except Exception as e:
                logger.error(f"Ошибка при отправке к Google: {e}", exc_info=True)
                socketio.emit('error', {'message': str(e)}, room=client_id)
        
        thread = threading.Thread(target=send_to_google, daemon=True)
        thread.start()
        
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
        emit('error', {'message': str(e)}, room=request.sid)

@socketio.on('init')
def handle_init(data):
    """Инициализация соединения с API ключом"""
    try:
        client_id = request.sid
        api_key = data.get('api_key') or data.get('apiKey')
        
        if not api_key:
            emit('error', {'message': 'API key required'}, room=client_id)
            return
        
        client_api_keys[client_id] = api_key
        logger.info(f"Инициализировано соединение {client_id} с API ключом: {api_key[:10]}...")
        
        # Создаем соединение к Google
        thread = threading.Thread(
            target=create_google_connection,
            args=(client_id, api_key),
            daemon=True
        )
        thread.start()
        
        emit('initialized', {'status': 'ok', 'client_id': client_id})
        
    except Exception as e:
        logger.error(f"Ошибка инициализации: {e}", exc_info=True)
        emit('error', {'message': str(e)}, room=request.sid)

# Flask routes
@app.route("/")
def home():
    """Главная страница"""
    proxy_config = get_proxy_config()
    return jsonify({
        "service": "WebSocket Proxy Server for Google Gemini Live API",
        "status": "running",
        "proxy": "configured" if proxy_config else "not configured",
        "proxy_host": f"{proxy_config['host']}:{proxy_config['port']}" if proxy_config else None,
        "websocket_endpoint": "/socket.io/",
        "info_endpoint": "/api/gemini/ws-proxy-info",
        "connection_method": "Socket.IO",
    })

@app.route("/health")
def health():
    """Health check endpoint для Render"""
    return jsonify({"status": "healthy"}), 200

@app.route("/api/gemini/ws-proxy-info", methods=["GET", "OPTIONS"])
def api_ws_proxy_info():
    """Возвращает информацию о WebSocket прокси для клиента"""
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        # Получаем API ключ из query параметров
        api_key = request.args.get('api_key')
        if not api_key:
            return jsonify({"error": "API key required"}), 400
        
        # Получаем базовый URL
        base_url = request.url_root.rstrip('/')
        ws_proxy_url = base_url.replace('http://', 'ws://').replace('https://', 'wss://') + '/socket.io/'
        
        return jsonify({
            "ws_proxy_url": ws_proxy_url,
            "api_key_masked": api_key[:10] + "..." if len(api_key) > 10 else "***",
            "proxy_configured": get_proxy_config() is not None,
            "connection_method": "Socket.IO",
        }), 200
        
    except Exception as e:
        logger.error(f"[WS Proxy Info] Ошибка: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

def run_server():
    """Запускает Flask сервер с SocketIO"""
    flask_port = int(os.getenv('PORT', '5000'))
    
    proxy_config = get_proxy_config()
    if proxy_config:
        logger.info(f"✅ HTTP прокси настроен: {proxy_config['host']}:{proxy_config['port']}")
    else:
        logger.warning("⚠️ HTTP прокси не настроен, подключение будет прямым")
    
    logger.info(f"Запуск Flask сервера с SocketIO на порту {flask_port}...")
    logger.info("📡 WebSocket доступен через Socket.IO: /socket.io/")
    logger.info("💡 Клиент должен использовать Socket.IO библиотеку для подключения")
    
    # Запускаем Flask с SocketIO через eventlet
    socketio.run(
        app,
        host='0.0.0.0',
        port=flask_port,
        debug=False,
        use_reloader=False,
        log_output=True
    )

if __name__ == "__main__":
    run_server()
