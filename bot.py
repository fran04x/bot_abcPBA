import os
import ssl
import html
import requests
import urllib3
import time
import threading
import signal
import sys
import atexit
import re
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# --- CONFIGURACIÓN ---
CUIL = os.environ.get("CUIL")
PASSWORD = os.environ.get("PASSWORD")
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
INSECURE_SSL = os.environ.get("INSECURE_SSL", "false").strip().lower() in {"1", "true", "yes"}
REQUEST_TIMEOUT = (10, 30)

try:
    TELEGRAM_BUTTON_COOLDOWN_SECONDS = int(os.environ.get("TELEGRAM_BUTTON_COOLDOWN_SECONDS", "60"))
    if TELEGRAM_BUTTON_COOLDOWN_SECONDS < 0:
        TELEGRAM_BUTTON_COOLDOWN_SECONDS = 0
except ValueError:
    TELEGRAM_BUTTON_COOLDOWN_SECONDS = 60

# Credenciales de Upstash Redis
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

# --- MEMORIA GLOBAL ---
CACHE_RESULTADOS = []
MENSAJES_ENVIADOS = set()
ULTIMA_CARGA_OK_TS = 0
CACHE_LOCK = threading.Lock()
FORZAR_REFRESH = threading.Event()
CALLBACKS_PROCESADOS = {}
MENSAJES_LOCK = threading.Lock()
CALLBACK_LOCK = threading.Lock()
UPSTASH_SESSION = requests.Session()
TELEGRAM_SESSION = requests.Session()
INSTANCE_LOCK_KEY = os.environ.get("INSTANCE_LOCK_KEY", "abcbot_instance_lock")
PASSIVE_INSTANCE_LOCK_KEY = os.environ.get("PASSIVE_INSTANCE_LOCK_KEY", f"{INSTANCE_LOCK_KEY}:passive")
LISTENER_LOCK_KEY = os.environ.get("LISTENER_LOCK_KEY", f"{INSTANCE_LOCK_KEY}:listener")
MONITOR_LOCK_KEY = os.environ.get("MONITOR_LOCK_KEY", f"{INSTANCE_LOCK_KEY}:monitor")
INSTANCE_OWNER = f"{os.environ.get('HOSTNAME', 'local')}-{os.getpid()}-{int(time.time())}"
BOT_SESSION_ID = os.environ.get("BOT_SESSION_ID", f"{int(time.time())}-{os.getpid()}")[-24:]
CALLBACK_GET_RESULTADOS = f"get_resultados:{BOT_SESSION_ID}"
INSTANCE_ADMISSION_MODE = None
INSTANCE_ADMISSION_LAST_RENEW_TS = 0.0
INSTANCE_ADMISSION_LOCK = threading.Lock()

# --- CANDADOS CORTOS PARA DEPLOYS RÁPIDOS ---
try:
    LOCK_TTL_SEG = int(os.environ.get("INSTANCE_LOCK_TTL_SECONDS", "300"))
    if LOCK_TTL_SEG < 300:
        LOCK_TTL_SEG = 300
except ValueError:
    LOCK_TTL_SEG = 300

try:
    LOCK_RENEW_INTERVAL_SECONDS = int(os.environ.get("LOCK_RENEW_INTERVAL_SECONDS", "180"))
except ValueError:
    LOCK_RENEW_INTERVAL_SECONDS = 180
if LOCK_RENEW_INTERVAL_SECONDS < 15:
    LOCK_RENEW_INTERVAL_SECONDS = 15
if LOCK_RENEW_INTERVAL_SECONDS >= LOCK_TTL_SEG:
    LOCK_RENEW_INTERVAL_SECONDS = max(15, LOCK_TTL_SEG - 5)

_refresh_hours_default = {6, 9, 12, 15, 18, 21}
try:
    _refresh_hours_raw = os.environ.get("TELEGRAM_REFRESH_ACTIVE_HOURS", "6,9,12,15,18,21")
    ACTIVE_REFRESH_HOURS = sorted({int(x.strip()) for x in _refresh_hours_raw.split(",") if x.strip()})
    ACTIVE_REFRESH_HOURS = [h for h in ACTIVE_REFRESH_HOURS if 0 <= h <= 23]
    if not ACTIVE_REFRESH_HOURS:
        ACTIVE_REFRESH_HOURS = sorted(_refresh_hours_default)
except Exception:
    ACTIVE_REFRESH_HOURS = sorted(_refresh_hours_default)

try:
    TELEGRAM_MAX_MESSAGE_LEN = int(os.environ.get("TELEGRAM_MAX_MESSAGE_LEN", "4096"))
    if TELEGRAM_MAX_MESSAGE_LEN < 1:
        raise ValueError()
except ValueError:
    TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_MAX_MESSAGE_LEN = min(TELEGRAM_MAX_MESSAGE_LEN, 4096)

try:
    POST_FETCH_GRACE_SECONDS = int(os.environ.get("POST_FETCH_GRACE_SECONDS", "0"))
    if POST_FETCH_GRACE_SECONDS < 0:
        POST_FETCH_GRACE_SECONDS = 0
    if POST_FETCH_GRACE_SECONDS > 120:
        POST_FETCH_GRACE_SECONDS = 120
except ValueError:
    POST_FETCH_GRACE_SECONDS = 0

SOLO_JORNADA_COMPLETA = os.environ.get("SOLO_JORNADA_COMPLETA", "false").strip().lower() == "true"

DOC_PARTICIPANTE_PRIORITARIO = "30426801"

if INSECURE_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Dashboard Activo")
        
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

# --- INFRAESTRUCTURA WEB / TRANSPORTE ---
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"[*] Servidor web escuchando en puerto {port}...", flush=True)
    server.serve_forever()

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        if INSECURE_SSL:
            context = create_urllib3_context()
            context.set_ciphers("ALL:@SECLEVEL=0")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
                context.options |= ssl.OP_LEGACY_SERVER_CONNECT
            kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

# --- TELEGRAM: ENVÍO / EDICIÓN / ARMADO DE MENSAJES ---
def enviar_telegram(mensaje, silencioso=True, con_boton=False, es_permanente=False):
    global MENSAJES_ENVIADOS
    if not TOKEN or not CHAT_ID:
        return []

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    sent_ids = []
    
    def enviar_parte(texto):
        nonlocal sent_ids
        def registrar_envio_exitoso(data):
            if not data.get("ok"):
                return
            message_id = data["result"]["message_id"]
            sent_ids.append(message_id)
            if not es_permanente:
                with MENSAJES_LOCK:
                    MENSAJES_ENVIADOS.add(message_id)
                upstash_cmd("sadd", "mensajes_borrables", message_id)

        payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True, "disable_notification": silencioso}
        
        if con_boton:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": "🌐 WEB ABC", "url": "https://misservicios.abc.gob.ar/actos.publicos.digitales/"}],
                    [{"text": "🔄 Actualizar resultados", "callback_data": CALLBACK_GET_RESULTADOS}]
                ]
            }
            
        payload_plain = {"chat_id": CHAT_ID, "text": texto, "disable_web_page_preview": True, "disable_notification": silencioso}
        if con_boton:
            payload_plain["reply_markup"] = payload["reply_markup"]

        try:
            response = TELEGRAM_SESSION.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            registrar_envio_exitoso(data)
            return
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                try:
                    resp_plain = TELEGRAM_SESSION.post(url, json=payload_plain, timeout=REQUEST_TIMEOUT)
                    resp_plain.raise_for_status()
                    data = resp_plain.json()
                    registrar_envio_exitoso(data)
                    return
                except Exception:
                    pass
            print(f"[-] Error enviando Telegram: {error}", flush=True)

    max_len = TELEGRAM_MAX_MESSAGE_LEN
    if len(mensaje) <= max_len:
        enviar_parte(mensaje)
        return sent_ids

    partes = []
    bloque = ""
    for linea in mensaje.splitlines(keepends=True):
        if len(bloque) + len(linea) <= max_len:
            bloque += linea
        else:
            partes.append(bloque)
            bloque = linea
    if bloque:
        partes.append(bloque)

    for idx, parte in enumerate(partes, start=1):
        enviar_parte(parte)
    return sent_ids

# --- UPSTASH: HELPERS, LOCKS Y DEDUPE DISTRIBUIDO ---
def _upstash_headers():
    if UPSTASH_URL and UPSTASH_TOKEN:
        return UPSTASH_URL.rstrip('/'), {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    return None, None

def upstash_cmd(*parts, timeout=5):
    base_url, headers = _upstash_headers()
    if not base_url:
        return None
    try:
        url = f"{base_url}/" + "/".join(str(p) for p in parts)
        resp = UPSTASH_SESSION.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return resp.json().get("result")
    except Exception:
        pass
    return None

def adquirir_lock_instancia(ttl_seg=LOCK_TTL_SEG, lock_key=INSTANCE_LOCK_KEY):
    base_url, _ = _upstash_headers()
    if not base_url:
        return True

    result = upstash_cmd("set", lock_key, INSTANCE_OWNER, "EX", ttl_seg, "NX")
    if str(result).upper() == "OK":
        return True

    owner_actual = upstash_cmd("get", lock_key)
    return owner_actual == INSTANCE_OWNER

def renovar_lock_instancia(ttl_seg=LOCK_TTL_SEG, lock_key=INSTANCE_LOCK_KEY):
    base_url, _ = _upstash_headers()
    if not base_url:
        return True

    # SET con XX (solo si existe) + GET devuelve el valor anterior en un solo round-trip
    owner_previo = upstash_cmd("set", lock_key, INSTANCE_OWNER, "EX", ttl_seg, "XX", "GET")

    # Si owner_previo es None, puede ser micro-corte de red o key expirada.
    # Si no es None y es diferente a nosotros, otra instancia tiene el lock.
    if owner_previo is not None and owner_previo != INSTANCE_OWNER:
        return False

    # Asumimos que sigue vivo para no matarlo por un parpadeo del WiFi de Render
    return True

def liberar_lock_instancia(lock_key=INSTANCE_LOCK_KEY):
    base_url, _ = _upstash_headers()
    if not base_url:
        return

    owner_actual = upstash_cmd("get", lock_key)
    if owner_actual == INSTANCE_OWNER:
        upstash_cmd("del", lock_key)

def _set_admission_mode(mode):
    global INSTANCE_ADMISSION_MODE
    with INSTANCE_ADMISSION_LOCK:
        INSTANCE_ADMISSION_MODE = mode

def _get_admission_mode():
    with INSTANCE_ADMISSION_LOCK:
        return INSTANCE_ADMISSION_MODE

def admitir_instancia(ttl_seg=LOCK_TTL_SEG):
    global INSTANCE_ADMISSION_LAST_RENEW_TS
    base_url, _ = _upstash_headers()
    if not base_url:
        _set_admission_mode("active")
        INSTANCE_ADMISSION_LAST_RENEW_TS = time.time()
        return True

    if adquirir_lock_instancia(ttl_seg, INSTANCE_LOCK_KEY):
        _set_admission_mode("active")
        INSTANCE_ADMISSION_LAST_RENEW_TS = time.time()
        return True

    if adquirir_lock_instancia(ttl_seg, PASSIVE_INSTANCE_LOCK_KEY):
        _set_admission_mode("passive")
        INSTANCE_ADMISSION_LAST_RENEW_TS = time.time()
        return True

    _set_admission_mode(None)
    return False

def mantener_admision_instancia(force=False, ttl_seg=LOCK_TTL_SEG):
    global INSTANCE_ADMISSION_LAST_RENEW_TS
    base_url, _ = _upstash_headers()
    if not base_url:
        return True

    ahora = time.time()
    if not force and (ahora - INSTANCE_ADMISSION_LAST_RENEW_TS) < LOCK_RENEW_INTERVAL_SECONDS:
        return True

    mode = _get_admission_mode()
    if mode == "active":
        key = INSTANCE_LOCK_KEY
    elif mode == "passive":
        key = PASSIVE_INSTANCE_LOCK_KEY
    else:
        return False

    if renovar_lock_instancia(ttl_seg, key):
        INSTANCE_ADMISSION_LAST_RENEW_TS = ahora
        return True

    if adquirir_lock_instancia(ttl_seg, key):
        INSTANCE_ADMISSION_LAST_RENEW_TS = ahora
        return True

    return False

def liberar_admision_instancia():
    mode = _get_admission_mode()
    if mode == "active":
        liberar_lock_instancia(INSTANCE_LOCK_KEY)
    elif mode == "passive":
        liberar_lock_instancia(PASSIVE_INSTANCE_LOCK_KEY)

# --- MANEJO DE CIERRE SEGURO (GRACEFUL SHUTDOWN) ---
def limpieza_salida():
    print("\n[!] Apagando instancia elegantemente. Liberando candados de Upstash...", flush=True)
    liberar_admision_instancia()
    liberar_lock_instancia(MONITOR_LOCK_KEY)
    liberar_lock_instancia(LISTENER_LOCK_KEY)

# Le decimos a Python que ejecute "limpieza_salida" justo antes de morir
atexit.register(limpieza_salida)

def manejar_senales(sig, frame):
    # Al llamar a sys.exit(0), forzamos a que salte el 'atexit' de arriba
    sys.exit(0)

# Atrapamos la orden de asesinato de Render (SIGTERM)
signal.signal(signal.SIGTERM, manejar_senales)
# Atrapamos el cierre manual por consola (Control+C)
signal.signal(signal.SIGINT, manejar_senales)

def callback_ya_procesado(callback_id, ttl_seg=300, max_items=2000):
    base_url, _ = _upstash_headers()
    if base_url:
        clave = f"cb_seen_{callback_id}"
        result = upstash_cmd("set", clave, INSTANCE_OWNER, "EX", ttl_seg, "NX")
        # CORRECCIÓN: Solo bloqueamos si estamos seguros de que otro bot lo procesó.
        # Si result es None (falla de red), lo dejamos pasar al fallback local.
        if result is not None and str(result).upper() != "OK":
            return True

    ahora = time.time()
    expirar = ahora - ttl_seg

    with CALLBACK_LOCK:
        for cb_id, ts in list(CALLBACKS_PROCESADOS.items()):
            if ts < expirar:
                CALLBACKS_PROCESADOS.pop(cb_id, None)

        if len(CALLBACKS_PROCESADOS) > max_items:
            cantidad = len(CALLBACKS_PROCESADOS) - max_items
            for cb_id, _ in sorted(CALLBACKS_PROCESADOS.items(), key=lambda kv: kv[1])[:cantidad]:
                CALLBACKS_PROCESADOS.pop(cb_id, None)

        if callback_id in CALLBACKS_PROCESADOS:
            return True

        CALLBACKS_PROCESADOS[callback_id] = ahora
        return False

def enviar_ofertas_sin_cortes(
    ofertas,
    encabezado=None,
    silencioso=True,
    es_permanente=False,
    repetir_encabezado=False,
    pausa_segundos=1,
    con_boton_al_final=False
):
    if not ofertas:
        if encabezado:
            enviar_telegram(encabezado, silencioso=silencioso, es_permanente=es_permanente, con_boton=con_boton_al_final)
        return

    max_len = TELEGRAM_MAX_MESSAGE_LEN
    prefijo = f"{encabezado}\n\n" if encabezado else ""
    mensaje_actual = ""
    envio_numero = 0

    def prefijo_para_nuevo_mensaje():
        if not prefijo: return ""
        if repetir_encabezado: return prefijo
        return prefijo if envio_numero == 0 else ""

    for idx, oferta in enumerate(ofertas):
        texto_oferta = str(oferta)
        inicio = prefijo_para_nuevo_mensaje()
        es_el_ultimo = (idx == len(ofertas) - 1)

        if not mensaje_actual:
            candidato = f"{inicio}{texto_oferta}"
            if len(candidato) <= max_len:
                mensaje_actual = candidato
                if es_el_ultimo:
                    enviar_telegram(mensaje_actual, silencioso=silencioso, es_permanente=es_permanente, con_boton=con_boton_al_final)
                continue

            if inicio and len(inicio.strip()) <= max_len:
                enviar_telegram(inicio.strip(), silencioso=silencioso, es_permanente=es_permanente)
                envio_numero += 1
                if pausa_segundos: time.sleep(pausa_segundos)

            enviar_telegram(texto_oferta, silencioso=silencioso, es_permanente=es_permanente, con_boton=(con_boton_al_final if es_el_ultimo else False))
            envio_numero += 1
            if pausa_segundos: time.sleep(pausa_segundos)
            continue

        if len(mensaje_actual) + len(texto_oferta) <= max_len:
            mensaje_actual += texto_oferta
            if es_el_ultimo:
                enviar_telegram(mensaje_actual, silencioso=silencioso, es_permanente=es_permanente, con_boton=con_boton_al_final)
            continue

        enviar_telegram(mensaje_actual, silencioso=silencioso, es_permanente=es_permanente)
        envio_numero += 1
        if pausa_segundos: time.sleep(pausa_segundos)

        inicio = prefijo_para_nuevo_mensaje()
        candidato = f"{inicio}{texto_oferta}"
        if len(candidato) <= max_len:
            mensaje_actual = candidato
            if es_el_ultimo:
                enviar_telegram(mensaje_actual, silencioso=silencioso, es_permanente=es_permanente, con_boton=con_boton_al_final)
        else:
            if inicio and len(inicio.strip()) <= max_len:
                enviar_telegram(inicio.strip(), silencioso=silencioso, es_permanente=es_permanente)
                envio_numero += 1
                if pausa_segundos: time.sleep(pausa_segundos)
            enviar_telegram(texto_oferta, silencioso=silencioso, es_permanente=es_permanente, con_boton=(con_boton_al_final if es_el_ultimo else False))
            envio_numero += 1
            if pausa_segundos: time.sleep(pausa_segundos)
            mensaje_actual = ""

def limpiar_chat():
    global MENSAJES_ENVIADOS
    if not TOKEN or not CHAT_ID:
        return
    url_delete = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
    
    # 1. Juntamos los de la memoria RAM actual
    with MENSAJES_LOCK:
        mensajes_a_borrar = set(MENSAJES_ENVIADOS)
    
    # 2. Rescatamos los fantasmas del bot anterior desde Upstash
    fantasmas = upstash_cmd("smembers", "mensajes_borrables")
    if fantasmas:
        for msg_id in fantasmas:
            try:
                mensajes_a_borrar.add(int(msg_id))
            except Exception:
                pass
                
    # 3. Borramos todos de Telegram
    mensajes_fallidos = set()
    for msg_id in list(mensajes_a_borrar):
        try:
            resp = TELEGRAM_SESSION.post(url_delete, json={"chat_id": CHAT_ID, "message_id": msg_id}, timeout=5)
            ok = False
            if resp.status_code == 200:
                try:
                    ok = bool(resp.json().get("ok"))
                except Exception:
                    ok = False
            if not ok:
                mensajes_fallidos.add(msg_id)
        except Exception:
            mensajes_fallidos.add(msg_id)
        time.sleep(0.05)

    # Reintento inmediato de los que fallaron (mejora limpieza en ciclos con rate-limit)
    if mensajes_fallidos:
        time.sleep(0.5)
        for msg_id in list(mensajes_fallidos):
            try:
                resp = TELEGRAM_SESSION.post(url_delete, json={"chat_id": CHAT_ID, "message_id": msg_id}, timeout=5)
                ok = False
                if resp.status_code == 200:
                    try:
                        ok = bool(resp.json().get("ok"))
                    except Exception:
                        ok = False
                if ok:
                    mensajes_fallidos.discard(msg_id)
            except Exception:
                pass
            time.sleep(0.05)
            
    # 4. Limpiamos la memoria y la base de datos
    with MENSAJES_LOCK:
        MENSAJES_ENVIADOS.clear()
        MENSAJES_ENVIADOS.update(mensajes_fallidos)
    upstash_cmd("del", "mensajes_borrables")
    for msg_id in mensajes_fallidos:
        upstash_cmd("sadd", "mensajes_borrables", msg_id)

# --- TELEGRAM: LISTENER DE BOTONES ---
def escuchar_botones():
    global CACHE_RESULTADOS, ULTIMA_CARGA_OK_TS

    if not TOKEN or not CHAT_ID:
        print("[!] Listener deshabilitado: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.", flush=True)
        while True:
            time.sleep(60)
    
    while True: # Bucle de supervivencia (Mantiene vivo el hilo si pierde el lock)
        if not adquirir_lock_instancia(LOCK_TTL_SEG, LISTENER_LOCK_KEY):
            print("[!] Listener pasivo: esperando a que se libere el lock...", flush=True)
            time.sleep(15)
            continue # Vuelve a intentar adquirir

        print("[*] Listener ACTIVO: Lock adquirido.", flush=True)
        lock_ultimo_renuevo_ts = time.time()
        offset = 0
        url_updates = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        url_answer = f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery"
        url_delete_webhook = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"
        ultimo_clic = 0

        try:
            TELEGRAM_SESSION.post(url_delete_webhook, json={"drop_pending_updates": False}, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            pass
        
        # Purga inicial rápida
        try:
            r = TELEGRAM_SESSION.get(url_updates, params={"offset": offset, "timeout": 5}, timeout=10)
            if r.status_code == 200:
                updates = r.json().get("result", [])
                if updates:
                    offset = updates[-1]["update_id"] + 1
        except Exception:
            pass
        
        try:
            while True: # Bucle de trabajo
                if not mantener_admision_instancia():
                    print("[!] Listener perdió admisión de instancia. Cerrando proceso.", flush=True)
                    sys.exit(0)

                ahora_lock = time.time()
                if (ahora_lock - lock_ultimo_renuevo_ts) >= LOCK_RENEW_INTERVAL_SECONDS:
                    if not renovar_lock_instancia(LOCK_TTL_SEG, LISTENER_LOCK_KEY):
                        print("[!] Listener perdió el lock. Volviendo a modo pasivo...", flush=True)
                        break
                    lock_ultimo_renuevo_ts = ahora_lock

                try:
                    r = TELEGRAM_SESSION.get(url_updates, params={"offset": offset, "timeout": 20}, timeout=30)
                    if r.status_code == 200:
                        updates = r.json().get("result", [])
                        for up in updates:
                            offset = up["update_id"] + 1
                            if "callback_query" in up:
                                cb = up["callback_query"]
                                cb_id = cb["id"]
                                data = cb.get("data")

                                if callback_ya_procesado(cb_id):
                                    TELEGRAM_SESSION.get(url_answer, params={"callback_query_id": cb_id, "text": "⏳ Ya procesado", "show_alert": False}, timeout=REQUEST_TIMEOUT)
                                    continue
                                
                                if data == CALLBACK_GET_RESULTADOS:
                                    ahora = time.time()
                                    segundos_desde_ultimo = ahora - ultimo_clic
                                    if segundos_desde_ultimo < TELEGRAM_BUTTON_COOLDOWN_SECONDS:
                                        restante = int(TELEGRAM_BUTTON_COOLDOWN_SECONDS - segundos_desde_ultimo)
                                        if restante < 1:
                                            restante = 1
                                        TELEGRAM_SESSION.get(
                                            url_answer,
                                            params={
                                                "callback_query_id": cb_id,
                                                "text": f"⏳ Esperá {restante}s para volver a actualizar.",
                                                "show_alert": False
                                            },
                                            timeout=REQUEST_TIMEOUT
                                        )
                                        continue
                                    
                                    ultimo_clic = ahora
                                    
                                    # CORRECCIÓN: SIEMPRE despertamos al bot, incluso si el ABC falló al arrancar
                                    FORZAR_REFRESH.set()
                                    
                                    if ULTIMA_CARGA_OK_TS == 0:
                                        TELEGRAM_SESSION.get(
                                            url_answer,
                                            params={
                                                "callback_query_id": cb_id,
                                                "text": "🔄 Intentando reconectar con el ABC...",
                                                "show_alert": False
                                            },
                                            timeout=REQUEST_TIMEOUT
                                        )
                                    else:
                                        TELEGRAM_SESSION.get(
                                            url_answer,
                                            params={
                                                "callback_query_id": cb_id,
                                                "text": "🔄 Consultando datos actualizados...",
                                                "show_alert": False
                                            },
                                            timeout=REQUEST_TIMEOUT
                                        )
                                elif isinstance(data, str) and data.startswith("get_resultados:"):
                                    TELEGRAM_SESSION.get(
                                        url_answer,
                                        params={
                                            "callback_query_id": cb_id,
                                            "text": "♻️ Bot reiniciado. Usá el botón del último listado.",
                                            "show_alert": False
                                        },
                                        timeout=REQUEST_TIMEOUT
                                    )
                except Exception as e:
                    print(f"[-] Error en bucle escuchar_botones: {e}", flush=True)
                    time.sleep(5)
        finally:
            liberar_lock_instancia(LISTENER_LOCK_KEY)

# --- ABC: EXTRACCIÓN / FORMATEO DE DATOS ---
def participante_es_objetivo(postulante, doc_objetivo):
    objetivo = re.sub(r"\D", "", str(doc_objetivo or ""))
    if not objetivo:
        return False

    posibles_campos = [
        "documento", "dni", "nrodocumento", "nro_documento", "numerodocumento",
        "nrodoc", "doc", "cuil", "cuit", "cuilcuit"
    ]

    for campo in posibles_campos:
        valor = postulante.get(campo)
        if valor in (None, ""):
            continue

        valores = valor if isinstance(valor, (list, tuple)) else [valor]
        for item in valores:
            texto = re.sub(r"\D", "", str(item))
            if not texto:
                continue
            if texto == objetivo:
                return True
            if len(texto) >= 10 and objetivo in texto:
                return True

    return False

def obtener_top_postulantes(session, id_oferta):
    url_p = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.postulante/select"
    params = {"q": f"idoferta:{id_oferta}", "sort": "puntaje desc", "rows": "10", "wt": "json"}
    try:
        r = session.get(url_p, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
        r.encoding = 'latin-1'
        if r.status_code == 200:
            docs = r.json().get("response", {}).get("docs", [])
            if not docs:
                return "Sin postulantes aún\n", False
            res = ""
            activos_mostrados = 0
            contiene_doc_objetivo = False
            for p in docs:
                es_participante_objetivo = participante_es_objetivo(p, DOC_PARTICIPANTE_PRIORITARIO)
                if not contiene_doc_objetivo and es_participante_objetivo:
                    contiene_doc_objetivo = True

                estado_post = str(p.get('estadopostulacion', '')).upper()
                designado = str(p.get('designado', '')).upper()
                if estado_post != "ACTIVA" or designado in ["S", "Y"]:
                    continue
                
                apellido = str(p.get('apellido', '')).strip().title()
                nombres = str(p.get('nombres', p.get('nombre', ''))).strip().title()

                # Si ABC agrupa todo en 'nombres', se asume formato "APELLIDO NOMBRE"
                if not apellido and nombres:
                    partes = nombres.split()
                    apellido = partes[0]
                    nombres = " ".join(partes[1:]) if len(partes) > 1 else ""

                primer_nombre = nombres.split()[0] if nombres else ""
                inicial = f"{primer_nombre[0]}." if primer_nombre else ""
                
                nombre_final = f"{inicial} {apellido}".strip() if apellido else "Docente"
                nombre_completo = html.escape(nombre_final)
                puntaje = html.escape(str(p.get('puntaje', '0.00')))
                puntaje_txt = f"{puntaje} pts"

                if es_participante_objetivo:
                    res += f"👉 {activos_mostrados + 1}º {nombre_completo} — {puntaje_txt}\n"
                else:
                    res += f"{activos_mostrados + 1}º {nombre_completo} — {puntaje_txt}\n"
                activos_mostrados += 1
                if activos_mostrados >= 3: break
                
            if activos_mostrados == 0:
                return "Postulantes inactivos (¡Vía libre!)\n", contiene_doc_objetivo
            return res, contiene_doc_objetivo
    except Exception:
        return "Error en ranking", False
    return "Sin datos", False

def formatear_fecha_argentina(valor, tz_obj):
    if valor in (None, "", "-"):
        return "N/A"

    if isinstance(valor, (list, tuple)):
        if not valor:
            return "N/A"
        valor = valor[0]

    try:
        texto = str(valor).strip()
        if not texto:
            return "N/A"

        # Si el sistema llega a mandar un timestamp numérico puro
        if texto.isdigit() or isinstance(valor, (int, float)):
            ts = float(valor)
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz_obj)
            return dt.strftime("%d/%m/%Y %H:%M")

        # Si manda el string ISO clásico ("2026-02-25T11:41:00Z")
        # Le arrancamos la "Z" o cualquier zona horaria y leemos la hora cruda
        texto_limpio = texto.replace("Z", "").split("+")[0]
        dt = datetime.fromisoformat(texto_limpio)
        
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return "N/A"
# --- LIMPIEZA DE CARACTERES EXCESIVOS EN DIRECCIÓN ---
def limpiar_direccion(texto):
    if not texto or str(texto).strip() in ("N/A", "-", ""):
        return "N/A"
    
    # 1. Colapsar múltiples espacios en uno solo
    t = re.sub(r'\s+', ' ', str(texto)).strip()
    
    # 2. Eliminar números idénticos repetidos al final (ej: "2730 2730")
    partes = t.split()
    if len(partes) >= 2 and partes[-1].isdigit() and partes[-1] == partes[-2]:
        t = " ".join(partes[:-1])
    
    # 3. Aplicar formato Título (Primera letra de cada palabra en mayúscula)
    t = t.title()
    
    # 4. Corregir abreviaturas específicas para que no queden extrañas
    # .title() convierte "E/" en "E/" o "Bº" en "Bº", pero "e/" queda mejor en minúscula
    t = t.replace(" E/ ", " e/ ").replace(" Bº", " Bº")
    
    return t

def envolver_texto_sin_cortar_palabras(texto, max_chars=18):
    if texto in (None, "", "N/A", "-"):
        return "N/A"

    limpio = re.sub(r'\s+', ' ', str(texto)).strip()
    if not limpio:
        return "N/A"

    palabras = limpio.split(" ")
    lineas = []
    linea_actual = ""

    for palabra in palabras:
        if not linea_actual:
            linea_actual = palabra
            continue

        candidato = f"{linea_actual} {palabra}"
        if len(candidato) <= max_chars:
            linea_actual = candidato
        else:
            lineas.append(linea_actual)
            linea_actual = palabra

    if linea_actual:
        lineas.append(linea_actual)

    return "\n".join(lineas)

def construir_url_oferta(id_oferta, id_detalle):
    base = "https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/"
    return f"{base}?oferta={id_oferta}&detalle={id_detalle}"

# --- MONITOREO PRINCIPAL ---
def monitorear():
    global CACHE_RESULTADOS, ULTIMA_CARGA_OK_TS
    limpieza_inicial_hecha = False
    linea_base_inicializada = False
    ofertas_conocidas_ids = set()
    ultimo_refresh_slot = None
    refresco_inicial_pendiente = True
    
    while True: # Bucle de supervivencia
        if not mantener_admision_instancia():
            print("[!] Se perdió la admisión de instancia. Cerrando proceso para evitar sobrepoblación.", flush=True)
            sys.exit(0)

        if not adquirir_lock_instancia(LOCK_TTL_SEG, MONITOR_LOCK_KEY):
            print("[!] Monitor pasivo: otra instancia está activa. Esperando pacientemente...", flush=True)
            time.sleep(15)
            continue

        print("[*] Monitor ACTIVO: Lock adquirido. Iniciando...", flush=True)
        lock_ultimo_renuevo_ts = time.time()

        if not limpieza_inicial_hecha:
            print("[*] Limpiando mensajes fantasmas de la sesión anterior...", flush=True)
            limpiar_chat()
            limpieza_inicial_hecha = True

        tz_ar = timezone(timedelta(hours=-3))

        try:
            while True: # Bucle de trabajo
                if not renovar_lock_instancia(LOCK_TTL_SEG, MONITOR_LOCK_KEY):
                    print("[!] Se perdió el lock de instancia. Deteniendo para evitar duplicados...", flush=True)
                    break # Vuelve al modo pasivo
                lock_ultimo_renuevo_ts = time.time()

                buffer_nuevas = []
                temp_cache = []
                temp_cache_ordenable = []
                refresh_forzado = FORZAR_REFRESH.is_set()
                FORZAR_REFRESH.clear()

                try:
                    with requests.Session() as session:
                        session.mount('https://', TLSAdapter())
                        session.headers.update({'User-Agent': 'Mozilla/5.0'})

                        login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
                        payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
                        session.post(login_url, data=payload, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)

                        url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
                        # 1. Agregamos el sort en la API para que no nos oculte las ofertas nuevas
                        params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND estado:"Publicada"', "rows": "1000", "wt": "json", "sort": "idoferta desc"}
                        r = session.get(url_solr, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
                        r.encoding = 'latin-1'

                        if r.status_code == 200:
                            docs = r.json().get("response", {}).get("docs", [])
                            hallazgos = [o for o in docs if "MAESTRO DE GRADO" in str(o.get("cargo", "")).upper() and "MG5" not in str(o.get("cargo", "")).upper()]
                            
                            # 2. Ordenamos internamente por fecha de inicio (De más antigua a más nueva)
                            def extraer_timestamp(valor):
                                if not valor or valor == "-": return 0.0
                                if isinstance(valor, (list, tuple)):
                                    if not valor: return 0.0
                                    valor = valor[0]
                                try:
                                    if isinstance(valor, (int, float)): return float(valor)
                                    texto = str(valor).strip()
                                    if not texto: return 0.0
                                    if texto.isdigit(): return float(texto)
                                    dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
                                    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                                    return dt.timestamp()
                                except Exception:
                                    return 0.0
                                    
                            hallazgos.sort(key=lambda x: extraer_timestamp(x.get('iniciooferta')))
                            # -------------------------------------------------------------------------
                            
                            procesados_en_vuelta = set()
                            ofertas_en_vuelta_ids = set()
                            ofertas_en_vuelta_detalle = {}

                            for info in hallazgos:
                                id_o = str(info.get('idoferta'))
                                ofertas_en_vuelta_ids.add(id_o)

                                if id_o in procesados_en_vuelta:
                                    continue
                                procesados_en_vuelta.add(id_o)

                                escuela = html.escape(str(info.get('escuela', 'N/A')))
                                curso_division = html.escape(str(info.get('cursodivision', '-')).strip())
                                direccion_raw = str(info.get('domiciliodesempeno', info.get('domicilio', 'N/A')))
                                direccion_formateada = envolver_texto_sin_cortar_palabras(limpiar_direccion(direccion_raw), max_chars=18)
                                direccion = html.escape(direccion_formateada)
                                revista_raw = str(info.get('supl_revista', '')).upper()
                                if revista_raw == 'S':
                                    revista = "Suplencia"
                                elif revista_raw == 'P':
                                    revista = "Provisionalidad"
                                else:
                                    revista = html.escape(revista_raw) if revista_raw else "N/A"
                                cierre_oferta = html.escape(formatear_fecha_argentina(info.get('finoferta'), tz_ar))
                                desde = html.escape(formatear_fecha_argentina(info.get('supl_desde'), tz_ar).split()[0])
                                hasta = html.escape(formatear_fecha_argentina(info.get('supl_hasta'), tz_ar).split()[0])

                                jornada_raw = str(info.get('jornada', '')).upper()
                                es_jornada_completa = ("JC" in jornada_raw) or ("COMPLETA" in jornada_raw)
                                es_jornada_simple = ("JS" in jornada_raw) or ("SIMPLE" in jornada_raw)

                                if SOLO_JORNADA_COMPLETA and not es_jornada_completa:
                                    continue

                                if es_jornada_completa:
                                    jornada_texto = "🔴 Completa"
                                elif es_jornada_simple:
                                    jornada_texto = "Simple"
                                else:
                                    jornada_texto = html.escape(jornada_raw) if jornada_raw else "N/A"

                                inicio_oferta_txt = formatear_fecha_argentina(info.get('iniciooferta'), tz_ar)
                                inicio_oferta = html.escape(inicio_oferta_txt)
                                inicio_oferta_ts = extraer_timestamp(info.get('iniciooferta'))

                                id_detalle = str(info.get('iddetalle', '')).strip()
                                if not id_detalle:
                                    id_detalle = "0"

                                ranking, contiene_doc_objetivo = obtener_top_postulantes(session, id_o)
                                url_oferta = html.escape(construir_url_oferta(id_o, id_detalle), quote=True)
                                txt = f"\n🏫 Escuela: {escuela}\n"
                                if direccion not in ("N/A", "-", ""):
                                    txt += f"📍 Dirección: {direccion}\n"
                                txt += "\n"
                                txt += "🕒 Oferta\n"
                                txt += f"• Inicio: {inicio_oferta}\n"
                                txt += f"• Cierre: {cierre_oferta}\n"
                                txt += "\n"
                                if curso_division not in ("-", "", "N/A"):
                                    txt += f"👥 Curso/Div: {curso_division}\n"
                                txt += f"⏱ Jornada: {jornada_texto}\n"
                                txt += f"📝 Revista: {revista}\n"
                                txt += "\n"
                                txt += "📅 Vigencia\n"
                                txt += f"• Desde: {desde}\n"
                                txt += f"• Hasta: {hasta}\n"
                                txt += "\n"
                                txt += "🏆 Puntajes\n"
                                txt += f"{ranking}"
                                txt += f"\n🌐 <a href=\"{url_oferta}\">Ver postulaciones</a>"

                                txt_listado = f"{txt}\n─────────────────────\n"

                                temp_cache_ordenable.append((contiene_doc_objetivo, inicio_oferta_ts, txt_listado))
                                ofertas_en_vuelta_detalle[id_o] = (txt, contiene_doc_objetivo, inicio_oferta_ts, es_jornada_completa)

                            temp_cache_ordenable.sort(key=lambda x: (x[0], x[1]))
                            temp_cache = [item[2] for item in temp_cache_ordenable]

                            with CACHE_LOCK:
                                CACHE_RESULTADOS = temp_cache
                            ULTIMA_CARGA_OK_TS = time.time()

                            if POST_FETCH_GRACE_SECONDS > 0:
                                time.sleep(POST_FETCH_GRACE_SECONDS)

                            if linea_base_inicializada:
                                ids_nuevos = ofertas_en_vuelta_ids - ofertas_conocidas_ids
                                for id_nuevo in ids_nuevos:
                                    detalle = ofertas_en_vuelta_detalle.get(id_nuevo)
                                    if detalle:
                                        txt, contiene_doc_objetivo, inicio_oferta_ts, es_jc = detalle
                                        buffer_nuevas.append((id_nuevo, txt, contiene_doc_objetivo, inicio_oferta_ts, es_jc))

                            ofertas_conocidas_ids = set(ofertas_en_vuelta_ids)
                            ahora = datetime.now(tz_ar)
                            slot_refresco = ahora.strftime("%Y-%m-%d-%H") if ahora.hour in ACTIVE_REFRESH_HOURS else None
                            refresco_programado = slot_refresco is not None and slot_refresco != ultimo_refresh_slot
                            debe_refrescar_telegram = refresh_forzado or refresco_programado or refresco_inicial_pendiente

                            if debe_refrescar_telegram:
                                limpiar_chat()
                                if temp_cache:
                                    enviar_ofertas_sin_cortes(
                                        temp_cache,
                                        encabezado=f"📊 Listado de cargos ({len(temp_cache)} resultados)",
                                        silencioso=True,
                                        es_permanente=False,
                                        repetir_encabezado=False,
                                        pausa_segundos=1,
                                        con_boton_al_final=True
                                    )
                                else:
                                    enviar_telegram("📭 Sin cargos activos en este momento.", silencioso=True, es_permanente=False, con_boton=True)
                                if slot_refresco is not None:
                                    ultimo_refresh_slot = slot_refresco
                                refresco_inicial_pendiente = False

                            hora_str = ahora.strftime("%H:%M")

                            if linea_base_inicializada and buffer_nuevas:
                                buffer_nuevas.sort(key=lambda x: (x[2], x[3]))
                                for id_o, txt, _, _, es_jc in buffer_nuevas:
                                    
                                    mensaje_nuevo = f"🚨 Nuevo cargo ({hora_str} hs)\n\n{txt}\n────────────────────────────────\n"
                                    
                                    # Solo emitirá sonido (silencioso=False) si es Jornada Completa.
                                    # De lo contrario, se envía silenciado.
                                    enviar_telegram(
                                        mensaje_nuevo, 
                                        silencioso=(not es_jc), 
                                        es_permanente=False
                                    )
                                    time.sleep(2)

                            if not linea_base_inicializada:
                                linea_base_inicializada = True
                                print("[*] Línea base inicial cargada. A partir de ahora solo se notifican cargos realmente nuevos.", flush=True)

                        else:
                            print(f"[!] Consulta devuelta con estado {r.status_code}", flush=True)

                    print(f"[*] Revisión finalizada ({datetime.now(tz_ar).strftime('%H:%M')}). Caché: {len(CACHE_RESULTADOS)}", flush=True)
                except Exception as e:
                    print(f"[-] Error: {e}", flush=True)

                # --- EL NUEVO SUEÑO LIGERO OPTIMIZADO ---
                lock_perdido = False

                for i in range(60):
                    desperto_por_boton = FORZAR_REFRESH.wait(timeout=15.0) 

                    if not mantener_admision_instancia():
                        print("[!] Se perdió la admisión de instancia durante espera. Cerrando proceso.", flush=True)
                        sys.exit(0)
                    
                    if desperto_por_boton:
                        break 
                    
                    ahora_lock = time.time()
                    if (ahora_lock - lock_ultimo_renuevo_ts) >= LOCK_RENEW_INTERVAL_SECONDS:
                        if not renovar_lock_instancia(LOCK_TTL_SEG, MONITOR_LOCK_KEY):
                            lock_perdido = True
                            break
                        lock_ultimo_renuevo_ts = ahora_lock
                        
                if lock_perdido:
                    print("[!] Lock caducó mientras dormía o fue robado. Saliendo de guardia...", flush=True)
                    break

        finally:
            liberar_lock_instancia(MONITOR_LOCK_KEY)

if __name__ == "__main__":
    if not admitir_instancia(LOCK_TTL_SEG):
        print("[!] Ya existe una instancia activa y una pasiva. Esta instancia se cerrará.", flush=True)
        sys.exit(0)

    print(f"[*] Admisión de instancia concedida en modo {_get_admission_mode()}.", flush=True)

    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    telegram_thread = threading.Thread(target=escuchar_botones, daemon=True)
    telegram_thread.start()
    
    time.sleep(5)
    
    monitorear()
