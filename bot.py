import os
import ssl
import html
import requests
import urllib3
import time
import threading
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

# Credenciales de Upstash Redis
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

# --- MEMORIA GLOBAL ---
CACHE_RESULTADOS = []
MENSAJES_ENVIADOS = set()
MENSAJE_POR_OFERTA = {}
ULTIMA_CARGA_OK_TS = 0
CACHE_LOCK = threading.Lock()
CALLBACKS_PROCESADOS = {}
INSTANCE_LOCK_KEY = os.environ.get("INSTANCE_LOCK_KEY", "abcbot_instance_lock")
LISTENER_LOCK_KEY = os.environ.get("LISTENER_LOCK_KEY", f"{INSTANCE_LOCK_KEY}:listener")
MONITOR_LOCK_KEY = os.environ.get("MONITOR_LOCK_KEY", f"{INSTANCE_LOCK_KEY}:monitor")
INSTANCE_OWNER = f"{os.environ.get('HOSTNAME', 'local')}-{os.getpid()}-{int(time.time())}"
BOT_SESSION_ID = os.environ.get("BOT_SESSION_ID", f"{int(time.time())}-{os.getpid()}")[-24:]
CALLBACK_GET_RESULTADOS = f"get_resultados:{BOT_SESSION_ID}"

try:
    LOCK_TTL_SEG = int(os.environ.get("INSTANCE_LOCK_TTL_SECONDS", "1800"))
    if LOCK_TTL_SEG < 300:
        LOCK_TTL_SEG = 300
except ValueError:
    LOCK_TTL_SEG = 1800

try:
    TELEGRAM_MAX_MESSAGE_LEN = int(os.environ.get("TELEGRAM_MAX_MESSAGE_LEN", "4096"))
    if TELEGRAM_MAX_MESSAGE_LEN < 1:
        raise ValueError()
except ValueError:
    TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_MAX_MESSAGE_LEN = min(TELEGRAM_MAX_MESSAGE_LEN, 4096)

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
def enviar_telegram(mensaje, silencioso=False, con_boton=False, es_permanente=False):
    global MENSAJES_ENVIADOS
    if not TOKEN or not CHAT_ID:
        return []

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    sent_ids = []
    
    def enviar_parte(texto):
        nonlocal sent_ids
        payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True, "disable_notification": silencioso}
        
        if con_boton:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "🔄 Obtener Resultados Actuales", "callback_data": CALLBACK_GET_RESULTADOS}]]
            }
            
        payload_plain = {"chat_id": CHAT_ID, "text": texto, "disable_web_page_preview": True, "disable_notification": silencioso}
        if con_boton:
            payload_plain["reply_markup"] = payload["reply_markup"]

        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if data.get("ok"):
                message_id = data["result"]["message_id"]
                sent_ids.append(message_id)
                if not es_permanente:
                    MENSAJES_ENVIADOS.add(message_id)
            return
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                try:
                    resp_plain = requests.post(url, json=payload_plain, timeout=REQUEST_TIMEOUT)
                    resp_plain.raise_for_status()
                    data = resp_plain.json()
                    if data.get("ok"):
                        message_id = data["result"]["message_id"]
                        sent_ids.append(message_id)
                        if not es_permanente:
                            MENSAJES_ENVIADOS.add(message_id)
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
        resp = requests.get(url, headers=headers, timeout=timeout)
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

    owner_actual = upstash_cmd("get", lock_key)
    if owner_actual != INSTANCE_OWNER:
        return False

    result = upstash_cmd("set", lock_key, INSTANCE_OWNER, "EX", ttl_seg, "XX")
    return str(result).upper() == "OK"

def liberar_lock_instancia(lock_key=INSTANCE_LOCK_KEY):
    base_url, _ = _upstash_headers()
    if not base_url:
        return

    owner_actual = upstash_cmd("get", lock_key)
    if owner_actual == INSTANCE_OWNER:
        upstash_cmd("del", lock_key)

def callback_ya_procesado(callback_id, ttl_seg=300, max_items=2000):
    base_url, _ = _upstash_headers()
    if base_url:
        clave = f"cb_seen_{callback_id}"
        result = upstash_cmd("set", clave, INSTANCE_OWNER, "EX", ttl_seg, "NX")
        if str(result).upper() != "OK":
            return True

    ahora = time.time()
    expirar = ahora - ttl_seg

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

def guardar_mensaje_oferta(id_oferta, message_id):
    MENSAJE_POR_OFERTA[str(id_oferta)] = int(message_id)
    base_url, headers = _upstash_headers()
    if not base_url:
        return
    try:
        requests.get(f"{base_url}/set/oferta_msg_{id_oferta}/{int(message_id)}", headers=headers, timeout=5)
    except Exception:
        pass

def obtener_mensaje_oferta(id_oferta):
    clave = str(id_oferta)
    if clave in MENSAJE_POR_OFERTA:
        return MENSAJE_POR_OFERTA[clave]

    base_url, headers = _upstash_headers()
    if not base_url:
        return None

    try:
        resp = requests.get(f"{base_url}/get/oferta_msg_{id_oferta}", headers=headers, timeout=5)
        if resp.status_code == 200:
            result = resp.json().get("result")
            if result not in (None, ""):
                MENSAJE_POR_OFERTA[clave] = int(result)
                return MENSAJE_POR_OFERTA[clave]
    except Exception:
        pass
    return None

def eliminar_mensaje_oferta(id_oferta):
    MENSAJE_POR_OFERTA.pop(str(id_oferta), None)
    base_url, headers = _upstash_headers()
    if not base_url:
        return
    try:
        requests.get(f"{base_url}/del/oferta_msg_{id_oferta}", headers=headers, timeout=5)
    except Exception:
        pass

def editar_mensaje_telegram(message_id, texto):
    if not TOKEN or not CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    payload = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    payload_plain = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": texto,
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200 and r.json().get("ok"):
            return True
        if 400 <= r.status_code < 500:
            r2 = requests.post(url, json=payload_plain, timeout=REQUEST_TIMEOUT)
            return r2.status_code == 200 and r2.json().get("ok")
    except Exception:
        return False
    return False

# --- TELEGRAM: UTILIDADES DE LISTADO Y LIMPIEZA ---
def enviar_ofertas_sin_cortes(
    ofertas,
    encabezado=None,
    silencioso=False,
    es_permanente=False,
    repetir_encabezado=False,
    pausa_segundos=1
):
    if not ofertas:
        if encabezado:
            enviar_telegram(encabezado, silencioso=silencioso, es_permanente=es_permanente)
        return

    max_len = TELEGRAM_MAX_MESSAGE_LEN
    prefijo = f"{encabezado}\n\n" if encabezado else ""

    mensaje_actual = ""
    envio_numero = 0

    def prefijo_para_nuevo_mensaje():
        if not prefijo:
            return ""
        if repetir_encabezado:
            return prefijo
        return prefijo if envio_numero == 0 else ""

    for oferta in ofertas:
        texto_oferta = str(oferta)
        inicio = prefijo_para_nuevo_mensaje()

        if not mensaje_actual:
            candidato = f"{inicio}{texto_oferta}"
            if len(candidato) <= max_len:
                mensaje_actual = candidato
                continue

            if inicio and len(inicio.strip()) <= max_len:
                enviar_telegram(inicio.strip(), silencioso=silencioso, es_permanente=es_permanente)
                envio_numero += 1
                if pausa_segundos:
                    time.sleep(pausa_segundos)

            enviar_telegram(texto_oferta, silencioso=silencioso, es_permanente=es_permanente)
            envio_numero += 1
            if pausa_segundos:
                time.sleep(pausa_segundos)
            continue

        if len(mensaje_actual) + len(texto_oferta) <= max_len:
            mensaje_actual += texto_oferta
            continue

        enviar_telegram(mensaje_actual, silencioso=silencioso, es_permanente=es_permanente)
        envio_numero += 1
        if pausa_segundos:
            time.sleep(pausa_segundos)

        inicio = prefijo_para_nuevo_mensaje()
        candidato = f"{inicio}{texto_oferta}"
        if len(candidato) <= max_len:
            mensaje_actual = candidato
        else:
            if inicio and len(inicio.strip()) <= max_len:
                enviar_telegram(inicio.strip(), silencioso=silencioso, es_permanente=es_permanente)
                envio_numero += 1
                if pausa_segundos:
                    time.sleep(pausa_segundos)
            enviar_telegram(texto_oferta, silencioso=silencioso, es_permanente=es_permanente)
            envio_numero += 1
            if pausa_segundos:
                time.sleep(pausa_segundos)
            mensaje_actual = ""

    if mensaje_actual:
        enviar_telegram(mensaje_actual, silencioso=silencioso, es_permanente=es_permanente)

def limpiar_chat():
    global MENSAJES_ENVIADOS
    url_delete = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
    for msg_id in list(MENSAJES_ENVIADOS):
        try:
            requests.post(url_delete, json={"chat_id": CHAT_ID, "message_id": msg_id}, timeout=5)
        except Exception:
            pass
    MENSAJES_ENVIADOS.clear()

# --- TELEGRAM: LISTENER DE BOTONES ---
def escuchar_botones():
    global CACHE_RESULTADOS, ULTIMA_CARGA_OK_TS
    if not adquirir_lock_instancia(LOCK_TTL_SEG, LISTENER_LOCK_KEY):
        print("[!] Listener pasivo: lock tomado por otra instancia.", flush=True)
        return

    offset = 0
    url_updates = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    url_answer = f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery"
    url_delete_webhook = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"
    ultimo_clic = 0

    try:
        requests.post(url_delete_webhook, json={"drop_pending_updates": False}, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[!] No se pudo eliminar webhook: {e}", flush=True)
    
    try:
        r = requests.get(url_updates, params={"offset": offset, "timeout": 5}, timeout=10)
        if r.status_code == 200:
            updates = r.json().get("result", [])
            if updates:
                offset = updates[-1]["update_id"] + 1
    except Exception:
        pass
    
    try:
        while True:
            if not renovar_lock_instancia(LOCK_TTL_SEG, LISTENER_LOCK_KEY):
                print("[!] Listener detiene: lock perdido.", flush=True)
                return

            try:
                r = requests.get(url_updates, params={"offset": offset, "timeout": 30}, timeout=40)
                if r.status_code == 200:
                    updates = r.json().get("result", [])
                    for up in updates:
                        offset = up["update_id"] + 1
                        if "callback_query" in up:
                            cb = up["callback_query"]
                            cb_id = cb["id"]
                            data = cb.get("data")

                            if callback_ya_procesado(cb_id):
                                requests.get(url_answer, params={"callback_query_id": cb_id, "text": "⏳ Ya procesado", "show_alert": False}, timeout=REQUEST_TIMEOUT)
                                continue
                            
                            if data == CALLBACK_GET_RESULTADOS:
                                ahora = time.time()
                                if ahora - ultimo_clic < 3:
                                    requests.get(url_answer, params={"callback_query_id": cb_id, "text": "⏳ Cargando...", "show_alert": False}, timeout=REQUEST_TIMEOUT)
                                    continue
                                
                                ultimo_clic = ahora
                                if ULTIMA_CARGA_OK_TS == 0:
                                    requests.get(
                                        url_answer,
                                        params={
                                            "callback_query_id": cb_id,
                                            "text": "⏳ El bot todavía está cargando datos. Intentá de nuevo en unos segundos.",
                                            "show_alert": False
                                        },
                                        timeout=REQUEST_TIMEOUT
                                    )
                                    continue

                                with CACHE_LOCK:
                                    cache_snapshot = list(CACHE_RESULTADOS)

                                if not cache_snapshot:
                                    requests.get(
                                        url_answer,
                                        params={
                                            "callback_query_id": cb_id,
                                            "text": "📭 No hay cargos activos en este momento.",
                                            "show_alert": False
                                        },
                                        timeout=REQUEST_TIMEOUT
                                    )
                                    continue

                                requests.get(url_answer, params={"callback_query_id": cb_id}, timeout=REQUEST_TIMEOUT)

                                limpiar_chat()
                                enviar_ofertas_sin_cortes(
                                    cache_snapshot,
                                    encabezado="📊 <b>LISTADO ACTUAL DE CARGOS PUBLICADOS:</b>",
                                    es_permanente=False,
                                    repetir_encabezado=False,
                                    pausa_segundos=1
                                )
                            elif isinstance(data, str) and data.startswith("get_resultados:"):
                                requests.get(
                                    url_answer,
                                    params={
                                        "callback_query_id": cb_id,
                                        "text": "♻️ Bot reiniciado. Usá el botón del último mensaje de inicio.",
                                        "show_alert": False
                                    },
                                    timeout=REQUEST_TIMEOUT
                                )
                else:
                    print(f"[!] getUpdates devolvió {r.status_code}: {r.text}", flush=True)
            except Exception as e:
                print(f"[-] Error en escuchar_botones: {e}", flush=True)
                time.sleep(5)
    finally:
        liberar_lock_instancia(LISTENER_LOCK_KEY)

# --- ABC: EXTRACCIÓN / FORMATEO DE DATOS ---
def obtener_top_postulantes(session, id_oferta):
    url_p = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.postulante/select"
    params = {"q": f"idoferta:{id_oferta}", "sort": "puntaje desc", "rows": "10", "wt": "json"}
    try:
        r = session.get(url_p, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            docs = r.json().get("response", {}).get("docs", [])
            if not docs: return "<i>Sin postulantes aún</i>"
            res = ""
            activos_mostrados = 0
            for p in docs:
                estado_post = str(p.get('estadopostulacion', '')).upper()
                designado = str(p.get('designado', '')).upper()
                if estado_post != "ACTIVA" or designado in ["S", "Y"]:
                    continue
                
                apellido = str(p.get('apellido', '')).strip()
                nombres = str(p.get('nombres', p.get('nombre', ''))).strip()
                nombre_crudo = f"{apellido} {nombres}".strip()
                
                if not nombre_crudo:
                    nombre_crudo = str(p.get('apellidoynombre', '')).strip()
                    
                if not nombre_crudo:
                    nombre_crudo = "Docente"
                    
                nombre_completo = html.escape(nombre_crudo.title())
                puntaje = html.escape(str(p.get('puntaje', '0.00')))
                
                res += f"  {activos_mostrados + 1}º {nombre_completo} | <b>{puntaje} pts</b>\n"
                activos_mostrados += 1
                if activos_mostrados >= 3: break
                
            if activos_mostrados == 0: return "<i>Postulantes inactivos (¡Vía libre!)</i>"
            return res
    except Exception:
        return "<i>Error en ranking</i>"
    return "<i>Sin datos</i>"

def formatear_fecha_argentina(valor, tz_obj):
    if valor in (None, "", "-"):
        return "N/A"

    if isinstance(valor, (list, tuple)):
        if not valor:
            return "N/A"
        valor = valor[0]

    try:
        if isinstance(valor, (int, float)):
            ts = float(valor)
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz_obj)
            return dt.strftime("%d/%m/%Y %H:%M")

        texto = str(valor).strip()
        if not texto:
            return "N/A"

        if texto.isdigit():
            ts = float(texto)
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz_obj)
            return dt.strftime("%d/%m/%Y %H:%M")

        dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(tz_obj)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return "N/A"

# --- MONITOREO PRINCIPAL ---
def monitorear():
    global CACHE_RESULTADOS, ULTIMA_CARGA_OK_TS
    if not adquirir_lock_instancia(LOCK_TTL_SEG, MONITOR_LOCK_KEY):
        print("[!] Otra instancia ya está monitoreando. Esta instancia queda en modo pasivo.", flush=True)
        return

    print("[*] Monitoreo inteligente iniciado...", flush=True)

    msg_arranque = (
        "✅ <b>SISTEMA INICIADO</b>\n\n"
        "El bot está activo y escaneando el ABC con estos filtros:\n"
        "📍 <b>Distrito:</b> General Pueyrredón\n"
        "📚 <b>Cargo:</b> Maestro de Grado\n"
        "⏱ <b>Jornada:</b> Simple y Completa\n"
        "📌 <b>Estado:</b> Ofertas 'Publicadas'\n\n"
        "🌐 <a href='https://misservicios.abc.gob.ar/actos.publicos.digitales/'>Simular búsqueda visual en el portal</a>\n"
        "<i>(Ingresá manualmente: Gral. Pueyrredón + Maestro de Grado)</i>\n\n"
        "👇 Podés pedir el listado actual tocando el botón de abajo."
    )
    enviar_telegram(msg_arranque, con_boton=True, es_permanente=True)

    ofertas_estados_local = {}
    HORAS_REPORTE = {6, 9, 14, 17, 20, 21}
    ultimo_reporte_enviado = None
    tz_ar = timezone(timedelta(hours=-3))

    try:
        while True:
            if not renovar_lock_instancia(LOCK_TTL_SEG, MONITOR_LOCK_KEY):
                print("[!] Se perdió el lock de instancia. Se detiene monitoreo para evitar duplicados.", flush=True)
                return

            buffer_nuevas = []
            buffer_cerradas = []
            temp_cache = []

            try:
                with requests.Session() as session:
                    session.mount('https://', TLSAdapter())
                    session.headers.update({'User-Agent': 'Mozilla/5.0'})

                    login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
                    payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
                    session.post(login_url, data=payload, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)

                    url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
                    params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND (estado:"Publicada" OR estado:"Designada")', "rows": "1000", "wt": "json"}
                    r = session.get(url_solr, params=params, verify=not INSECURE_SSL, timeout=REQUEST_TIMEOUT)

                    if r.status_code == 200:
                        docs = r.json().get("response", {}).get("docs", [])
                        hallazgos = [o for o in docs if "MAESTRO DE GRADO" in str(o.get("cargo", "")).upper()]
                        ts = int(time.time() * 1000)

                        procesados_en_vuelta = set()

                        for info in hallazgos:
                            id_o = str(info.get('idoferta'))

                            if id_o in procesados_en_vuelta:
                                continue
                            procesados_en_vuelta.add(id_o)

                            estado_actual = str(info.get('estado', '')).upper()
                            estado_previo = None

                            if UPSTASH_URL and UPSTASH_TOKEN:
                                base_url = UPSTASH_URL.rstrip('/')
                                headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
                                try:
                                    resp = requests.get(f"{base_url}/get/oferta_{id_o}", headers=headers, timeout=5)
                                    if resp.status_code == 200:
                                        estado_previo = resp.json().get("result")
                                except Exception:
                                    estado_previo = ofertas_estados_local.get(id_o)
                            else:
                                estado_previo = ofertas_estados_local.get(id_o)

                            es_nueva_y_publicada = False
                            cambio_a_designada = False

                            if not estado_previo:
                                if estado_actual == "PUBLICADA":
                                    es_nueva_y_publicada = True
                            else:
                                if estado_previo == "PUBLICADA" and estado_actual == "DESIGNADA":
                                    cambio_a_designada = True

                            if not estado_previo or (estado_previo != estado_actual):
                                if UPSTASH_URL and UPSTASH_TOKEN:
                                    try:
                                        requests.get(f"{base_url}/set/oferta_{id_o}/{estado_actual}", headers=headers, timeout=5)
                                    except Exception:
                                        ofertas_estados_local[id_o] = estado_actual
                                else:
                                    ofertas_estados_local[id_o] = estado_actual

                            escuela = html.escape(str(info.get('escuela', 'N/A')))
                            cargo = html.escape(str(info.get('cargo', 'N/A')))
                            curso = html.escape(str(info.get('curso', '-')))
                            division = html.escape(str(info.get('division', '-')))

                            jornada_raw = str(info.get('jornada', '')).upper()
                            if "JC" in jornada_raw:
                                jornada_texto = "Completa"
                            elif "JS" in jornada_raw:
                                jornada_texto = "Simple"
                            else:
                                jornada_texto = html.escape(jornada_raw) if jornada_raw else "N/A"

                            inicio_oferta_txt = formatear_fecha_argentina(info.get('iniciooferta'), tz_ar)
                            inicio_oferta = html.escape(inicio_oferta_txt)

                            if estado_actual == "PUBLICADA":
                                ranking = obtener_top_postulantes(session, id_o)
                                link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={info.get('iddetalle', id_o)}&_t={ts}"

                                txt = f"🏫 <b>Escuela:</b> {escuela}\n"
                                txt += f"📚 <b>Área:</b> <code>{cargo}</code>\n"
                                txt += f"🕒 <b>Inicio Oferta:</b> {inicio_oferta}\n"
                                if curso != "-" or division != "-":
                                    txt += f"👥 <b>Curso/Div:</b> {curso} - {division}\n"
                                txt += f"⏱ <b>Jornada:</b> {jornada_texto}\n"
                                txt += f"🏆 <b>Puntajes:</b>\n{ranking}"
                                txt += f"🔗 <a href=\"{html.escape(link, quote=True)}\">VER ESCUELA</a>\n"
                                txt += "───────────────────\n"

                                temp_cache.append(txt)

                                if es_nueva_y_publicada:
                                    buffer_nuevas.append((id_o, txt))

                            elif cambio_a_designada:
                                txt = f"🏫 <b>Escuela:</b> {escuela}\n"
                                txt += f"📚 <b>Área:</b> <code>{cargo}</code>\n"
                                txt += f"🕒 <b>Inicio Oferta:</b> {inicio_oferta}\n"
                                if curso != "-" or division != "-":
                                    txt += f"👥 <b>Curso/Div:</b> {curso} - {division}\n"
                                txt += f"⏱ <b>Jornada:</b> {jornada_texto}\n"
                                txt += "───────────────────\n"
                                buffer_cerradas.append((id_o, txt))

                        with CACHE_LOCK:
                            CACHE_RESULTADOS = temp_cache
                        ULTIMA_CARGA_OK_TS = time.time()

                        ahora = datetime.now(tz_ar)
                        hora_actual = ahora.hour
                        hora_str = ahora.strftime("%H:%M")

                        if buffer_nuevas:
                            for id_o, txt in buffer_nuevas:
                                mensaje_nuevo = f"🚨 <b>NUEVO CARGO ({hora_str} hs)</b> 🚨\n\n{txt}"
                                sent_ids = enviar_telegram(mensaje_nuevo, es_permanente=False)
                                if sent_ids:
                                    guardar_mensaje_oferta(id_o, sent_ids[0])
                                time.sleep(2)

                        if buffer_cerradas:
                            for id_o, txt in buffer_cerradas:
                                mensaje_designada = f"❌ <b>CARGO DESIGNADO ({hora_str} hs)</b> ❌\n\n{txt}"
                                message_id = obtener_mensaje_oferta(id_o)
                                editado = False
                                if message_id is not None:
                                    editado = editar_mensaje_telegram(message_id, mensaje_designada)

                                if not editado:
                                    enviar_telegram(mensaje_designada, silencioso=True, es_permanente=False)

                                eliminar_mensaje_oferta(id_o)
                                time.sleep(2)

                        if (buffer_nuevas or buffer_cerradas) and hora_actual in HORAS_REPORTE:
                            ultimo_reporte_enviado = hora_actual
                        elif not buffer_nuevas and not buffer_cerradas and hora_actual in HORAS_REPORTE and ultimo_reporte_enviado != hora_actual:
                            enviar_telegram(f"⏳ <i>{hora_str} hs - Bot activo: Monitoreando sin novedades por el momento.</i>", silencioso=True, es_permanente=False)
                            ultimo_reporte_enviado = hora_actual
                    else:
                        print(f"[!] Consulta devuelta con estado {r.status_code}", flush=True)

                print(f"[*] Revisión finalizada ({datetime.now(tz_ar).strftime('%H:%M')}). Caché: {len(CACHE_RESULTADOS)}", flush=True)
            except Exception as e:
                print(f"[-] Error: {e}", flush=True)

            time.sleep(900)
    finally:
        liberar_lock_instancia(MONITOR_LOCK_KEY)

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    telegram_thread = threading.Thread(target=escuchar_botones, daemon=True)
    telegram_thread.start()
    
    time.sleep(5)
    
    monitorear()
