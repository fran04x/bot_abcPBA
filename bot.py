import os
import ssl
import requests
import urllib3
import time
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURACIÓN ---
CUIL = os.environ.get("CUIL")
PASSWORD = os.environ.get("PASSWORD")
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Live-Dashboard Activo")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

# --- FUNCIONES DE TELEGRAM ---
def enviar_telegram(mensaje, silencioso=False):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": mensaje, 
        "parse_mode": "Markdown", 
        "disable_web_page_preview": True,
        "disable_notification": silencioso
    }
    try:
        r = requests.post(url, json=payload).json()
        if r.get("ok"):
            # Devolvemos el ID del mensaje para poder editarlo después
            return r["result"]["message_id"]
    except:
        pass
    return None

def editar_mensaje_telegram(message_id, nuevo_texto):
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    payload = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": nuevo_texto,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload)
    except:
        pass

# --- FUNCIÓN DE RANKING (FILTRANDO INACTIVOS) ---
def obtener_top_postulantes(session, id_oferta):
    url_p = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.postulante/select"
    params = {"q": f"idoferta:{id_oferta}", "sort": "puntaje desc", "rows": "10", "wt": "json"}
    try:
        r = session.get(url_p, params=params, verify=False)
        if r.status_code == 200:
            docs = r.json().get("response", {}).get("docs", [])
            if not docs: return "_Sin postulantes aún_"
            
            res = ""
            activos = 0
            for p in docs:
                est = str(p.get('estadopostulacion', '')).upper()
                desig = str(p.get('designado', '')).upper()
                
                if est != "ACTIVA" or desig in ["S", "Y"]: continue
                
                nombre = f"{p.get('apellido','')} {p.get('nombre','')}".title()
                activos += 1
                res += f"  {activos}º {nombre} | *{p.get('puntaje','0.00')} pts*\n"
                if activos >= 3: break
                
            if activos == 0: return "_Postulantes inactivos (¡Vía libre!)_"
            return res
    except: return "_Error en ranking_"
    return "_Sin datos_"

# --- MOTOR PRINCIPAL ---
def monitorear():
    print("[*] Dashboard en vivo iniciado...", flush=True)
    ofertas_avisadas = set()
    buffer_ofertas = []
    ultimo_turno = None 
    tz_ar = timezone(timedelta(hours=-3))
    
    # --- MEMORIA RAM DEL BOT ---
    mensajes_enviados = {} # {msg_id: "Texto completo del mensaje"}
    ofertas_mapeo = {} # {id_oferta: {"msg_id": 123, "link": "string exacto a reemplazar"}}
    
    while True:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        try:
            # Login
            login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
            payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
            session.post(login_url, data=payload, verify=False)
            
            # ATENCIÓN: Ahora pedimos Publicadas Y Designadas juntas
            url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
            query = 'descdistrito:"GENERAL PUEYRREDON" AND (estado:"Publicada" OR estado:"Designada")'
            params = {"q": query, "rows": "1000", "wt": "json"}
            r = session.get(url_solr, params=params, verify=False)
            
            if r.status_code == 200:
                docs = r.json().get("response", {}).get("docs", [])
                ts = int(time.time() * 1000)

                for info in docs:
                    cargo_str = str(info.get("cargo","")).upper()
                    if "MAESTRO DE GRADO" not in cargo_str: continue
                        
                    id_o = info.get('idoferta')
                    estado = str(info.get('estado', '')).upper()
                    
                    # CASO 1: Cargo nuevo para postularse
                    if estado == "PUBLICADA":
                        if id_o not in ofertas_avisadas:
                            ranking = obtener_top_postulantes(session, id_o)
                            link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={info.get('iddetalle', id_o)}&_t={ts}"
                            
                            # Guardamos la línea exacta para poder buscarla y borrarla después
                            linea_link = f"🔗 [POSTULARSE]({link})"
                            
                            texto = f"🏫 **Escuela:** {info.get('escuela')}\n"
                            texto += f"📚 **Área:** `{info.get('cargo')}`\n"
                            texto += f"🏆 **Top 3:**\n{ranking}"
                            texto += f"{linea_link}\n"
                            texto += "───────────────────\n"
                            
                            buffer_ofertas.append({"id": id_o, "texto": texto, "link_exacto": linea_link})
                            ofertas_avisadas.add(id_o)
                            
                    # CASO 2: El cargo se cerró y alguien lo agarró
                    elif estado == "DESIGNADA":
                        if id_o in ofertas_mapeo:
                            datos = ofertas_mapeo[id_o]
                            msg_id = datos["msg_id"]
                            link_viejo = datos["link"]
                            
                            if msg_id in mensajes_enviados:
                                texto_actual = mensajes_enviados[msg_id]
                                # Reemplazamos el link por el aviso de cerrado
                                nuevo_texto = texto_actual.replace(link_viejo, "❌ **[CARGO YA DESIGNADO]**")
                                
                                editar_mensaje_telegram(msg_id, nuevo_texto)
                                mensajes_enviados[msg_id] = nuevo_texto 
                            
                            # Ya no hace falta vigilarlo
                            del ofertas_mapeo[id_o]

                # --- LÓGICA DE ENVÍO POR TURNOS (9 AM y 9 PM) ---
                ahora = datetime.now(tz_ar)
                hora_actual = ahora.hour
                es_hora = (hora_actual == 9 and ultimo_turno != 9) or (hora_actual == 21 and ultimo_turno != 21)
                
                if es_hora:
                    hora_str = ahora.strftime("%H:%M")
                    if not buffer_ofertas:
                        enviar_telegram(f"⏳ _{hora_str} hs - Resumen: Sin cargos nuevos._", silencioso=True)
                    else:
                        bloque_texto = f"🚨 **RESUMEN DE CARGOS ({hora_str} hs)** 🚨\n\n"
                        datos_bloque = []
                        contador = 0
                        
                        for obj in buffer_ofertas:
                            bloque_texto += obj["texto"]
                            datos_bloque.append(obj)
                            contador += 1
                            
                            if contador >= 15:
                                msg_id = enviar_telegram(bloque_texto)
                                if msg_id:
                                    mensajes_enviados[msg_id] = bloque_texto
                                    for d in datos_bloque:
                                        ofertas_mapeo[d["id"]] = {"msg_id": msg_id, "link": d["link_exacto"]}
                                
                                bloque_texto = f"🚨 **RESUMEN CONTINUACIÓN** 🚨\n\n"
                                datos_bloque = []
                                contador = 0
                                time.sleep(2)
                                
                        if contador > 0:
                            msg_id = enviar_telegram(bloque_texto)
                            if msg_id:
                                mensajes_enviados[msg_id] = bloque_texto
                                for d in datos_bloque:
                                    ofertas_mapeo[d["id"]] = {"msg_id": msg_id, "link": d["link_exacto"]}
                        
                        buffer_ofertas.clear()
                    ultimo_turno = hora_actual

        except Exception as e:
            pass
        time.sleep(900)

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    time.sleep(5)
    monitorear()
