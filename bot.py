import os
import ssl
import html
import requests
import urllib3
import time
import threading
from collections import deque
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

def enviar_telegram(mensaje, silencioso=False, con_boton=False, es_permanente=False):
    global MENSAJES_ENVIADOS
    if not TOKEN or not CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    
    def enviar_parte(texto):
        payload = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True, "disable_notification": silencioso}
        
        if con_boton:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "🔄 Obtener Resultados Actuales", "callback_data": "get_resultados"}]]
            }
            
        payload_plain = {"chat_id": CHAT_ID, "text": texto, "disable_web_page_preview": True, "disable_notification": silencioso}
        if con_boton:
            payload_plain["reply_markup"] = payload["reply_markup"]

        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if data.get("ok") and not es_permanente:
                MENSAJES_ENVIADOS.add(data["result"]["message_id"])
            return
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                try:
                    resp_plain = requests.post(url, json=payload_plain, timeout=REQUEST_TIMEOUT)
                    resp_plain.raise_for_status()
                    data = resp_plain.json()
                    if data.get("ok") and not es_permanente:
                        MENSAJES_ENVIADOS.add(data["result"]["message_id"])
                    return
                except:
                    pass
            print(f"[-] Error enviando Telegram: {error}", flush=True)

    max_len = TELEGRAM_MAX_MESSAGE_LEN
    if len(mensaje) <= max_len:
        enviar_parte(mensaje)
        return

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

def limpiar_chat():
    global MENSAJES_ENVIADOS
    url_delete = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
    for msg_id in list(MENSAJES_ENVIADOS):
        try:
            requests.post(url_delete, json={"chat_id": CHAT_ID, "message_id": msg_id}, timeout=5)
        except:
            pass
    MENSAJES_ENVIADOS.clear()

def escuchar_botones():
    global CACHE_RESULTADOS
    offset = 0
    url_updates = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    url_answer = f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery"
    ultimo_clic = 0
    
    try:
        r = requests.get(url_updates, params={"offset": offset, "timeout": 5}, timeout=10)
        if r.status_code == 200:
            updates = r.json().get("result", [])
            if updates:
                offset = updates[-1]["update_id"] + 1
    except:
        pass
    
    while True:
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
                        
                        if data == "get_resultados":
                            ahora = time.time()
                            if ahora - ultimo_clic < 3:
                                requests.get(url_answer, params={"callback_query_id": cb_id, "text": "⏳ Cargando...", "show_alert": False})
                                continue
                            
                            ultimo_clic = ahora
                            requests.get(url_answer, params={"callback_query_id": cb_id})
                            
                            limpiar_chat()
                            
                            if not CACHE_RESULTADOS:
                                enviar_telegram("⏳ <i>El bot recién inició o no hay cargos activos. Intentá de nuevo en unos minutos.</i>", silencioso=True, es_permanente=False)
                            else:
                                enviar_telegram("📊 <b>LISTADO ACTUAL DE CARGOS PUBLICADOS:</b>", es_permanente=False)
                                bloque = ""
                                for idx, txt in enumerate(CACHE_RESULTADOS, 1):
                                    bloque += txt
                                    if idx % 10 == 0 or idx == len(CACHE_RESULTADOS):
                                        poner_boton = (idx == len(CACHE_RESULTADOS))
                                        enviar_telegram(bloque, con_boton=poner_boton, es_permanente=False)
                                        bloque = ""
                                        time.sleep(1) 
        except Exception as e:
            time.sleep(5)

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
    except:
        return "<i>Error en ranking</i>"
    return "<i>Sin datos</i>"

def monitorear():
    global CACHE_RESULTADOS
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
    
    while True:
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
                    hallazgos = [o for o in docs if "MAESTRO DE GRADO" in str(o.get("cargo","")).upper()]
                    ts = int(time.time() * 1000)

                    # --- ESCUDO ANTI-CLONES ---
                    procesados_en_vuelta = set()

                    for info in hallazgos:
                        id_o = str(info.get('idoferta'))
                        
                        # Si ya procesamos este ID en este mismo ciclo de búsqueda, lo saltamos
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
                            except:
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
                                except:
                                    ofertas_estados_local[id_o] = estado_actual
                            else:
                                ofertas_estados_local[id_o] = estado_actual

                        escuela = html.escape(str(info.get('escuela', 'N/A')))
                        cargo = html.escape(str(info.get('cargo', 'N/A')))
                        
                        # --- VOLVEMOS A EXTRAER CURSO Y DIVISIÓN ---
                        curso = html.escape(str(info.get('curso', '-')))
                        division = html.escape(str(info.get('division', '-')))
                        
                        jornada_raw = str(info.get('jornada', '')).upper()
                        if "JC" in jornada_raw:
                            jornada_texto = "Completa"
                        elif "JS" in jornada_raw:
                            jornada_texto = "Simple"
                        else:
                            jornada_texto = html.escape(jornada_raw) if jornada_raw else "N/A"
                        
                        if estado_actual == "PUBLICADA":
                            ranking = obtener_top_postulantes(session, id_o)
                            link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={info.get('iddetalle', id_o)}&_t={ts}"
                            
                            txt = f"🏫 <b>Escuela:</b> {escuela}\n"
                            txt += f"📋 <b>ID Oferta:</b> <code>{id_o}</code>\n"
                            txt += f"📚 <b>Área:</b> <code>{cargo}</code>\n"
                            
                            # Mostrar Curso/División solo si hay datos útiles
                            if curso != "-" or division != "-":
                                txt += f"👥 <b>Curso/Div:</b> {curso} - {division}\n"
                                
                            txt += f"⏱ <b>Jornada:</b> {jornada_texto}\n"
                            txt += f"🏆 <b>Puntajes:</b>\n{ranking}"
                            txt += f"🔗 <a href=\"{html.escape(link, quote=True)}\">VER ESCUELA</a>\n"
                            txt += "───────────────────\n"
                            
                            temp_cache.append(txt)
                            
                            if es_nueva_y_publicada:
                                buffer_nuevas.append(txt)

                        elif cambio_a_designada:
                            txt = f"🏫 <b>Escuela:</b> {escuela}\n"
                            txt += f"📋 <b>ID Oferta:</b> <code>{id_o}</code>\n"
                            txt += f"📚 <b>Área:</b> <code>{cargo}</code>\n"
                            if curso != "-" or division != "-":
                                txt += f"👥 <b>Curso/Div:</b> {curso} - {division}\n"
                            txt += f"⏱ <b>Jornada:</b> {jornada_texto}\n"
                            txt += "───────────────────\n"
                            buffer_cerradas.append(txt)

                    CACHE_RESULTADOS = temp_cache

                    ahora = datetime.now(tz_ar)
                    hora_actual = ahora.hour
                    hora_str = ahora.strftime("%H:%M")

                    if buffer_nuevas:
                        bloque = ""
                        for idx, txt in enumerate(buffer_nuevas, 1):
                            bloque += txt
                            if idx % 10 == 0 or idx == len(buffer_nuevas):
                                enviar_telegram(f"🚨 <b>NUEVOS CARGOS ({hora_str} hs)</b> 🚨\n\n{bloque}", es_permanente=False)
                                bloque = ""
                                time.sleep(2)
                                
                    if buffer_cerradas:
                        bloque = ""
                        for idx, txt in enumerate(buffer_cerradas, 1):
                            bloque += txt
                            if idx % 15 == 0 or idx == len(buffer_cerradas):
                                enviar_telegram(f"❌ <b>CARGOS DESIGNADOS (Cerrados)</b> ❌\n\n{bloque}", silencioso=True, es_permanente=False)
                                bloque = ""
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

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    telegram_thread = threading.Thread(target=escuchar_botones, daemon=True)
    telegram_thread.start()
    
    time.sleep(5)
    
    monitorear()
