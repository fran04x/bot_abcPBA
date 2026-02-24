import os
import ssl
import requests
import urllib3
import time
import threading
import json
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
        self.wfile.write(b"Bot Consolidado Activo")

def run_web_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

# --- ENVÍO DE MENSAJE GRUPAL ---
def enviar_telegram_grupal(mensaje, lista_botones):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown",
        "reply_markup": json.dumps({"inline_keyboard": lista_botones})
    }
    try:
        requests.post(url, json=payload)
    except:
        pass

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

def monitorear():
    print("[*] Monitoreo consolidado iniciado...", flush=True)
    # No mandamos mensaje de inicio para no spamear, solo cuando encuentre algo real
    
    ofertas_avisadas = set()
    
    while True:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
        session.headers.update({'User-Agent': 'Mozilla/5.0'})
        
        try:
            # Login
            login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
            payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}
            session.post(login_url, data=payload, verify=False)
            
            # Consulta
            url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
            params = {"q": 'descdistrito:"GENERAL PUEYRREDON" AND estado:"Designada"', "rows": "1000", "wt": "json"}
            r = session.get(url_solr, params=params, verify=False)
            
            if r.status_code == 200:
                docs = r.json().get("response", {}).get("docs", [])
                nuevos_hallazgos = []
                
                for oferta in docs:
                    cargo = str(oferta.get("cargo", "")).upper()
                    jornada = str(oferta.get("jornada", "")).upper()
                    
                    if "MAESTRO DE GRADO" in cargo and jornada == "JC":
                        id_o = oferta.get("idoferta")
                        if id_o not in ofertas_avisadas:
                            nuevos_hallazgos.append(oferta)
                            ofertas_avisadas.add(id_o)

                # Si hay algo nuevo, armamos EL mensaje único
                if nuevos_hallazgos:
                    texto_final = "🚨 **¡NUEVAS OFERTAS OBTENIDAS!** 🚨\n\n"
                    botones_finales = []
                    ts = int(time.time() * 1000)
                    
                    for i, cargo_info in enumerate(nuevos_hallazgos, 1):
                        escuela = cargo_info.get('escuela', 'N/A')
                        area = cargo_info.get('cargo', 'N/A')
                        curso = cargo_info.get('curso', 'N/A')
                        division = cargo_info.get('division', 'N/A')
                        id_o = cargo_info.get('idoferta')
                        id_d = cargo_info.get('iddetalle') or id_o
                        
                        texto_final += f"📍 **OFERTA #{i}**\n"
                        texto_final += f"🏫 Escuela: {escuela}\n"
                        texto_final += f"📚 Área: `{area}`\n"
                        texto_final += f"👥 Curso/Div: {curso} - {division}\n"
                        texto_final += f"📅 Toma: {cargo_info.get('tomaposesion', '')[:10]}\n"
                        texto_final += "--------------------------\n"
                        
                        # Creamos un botón por cada oferta
                        link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={id_d}&_t={ts}"
                        botones_finales.append([{"text": f"✅ VER POSTULANTES #{i}", "url": link}])
                    
                    enviar_telegram_grupal(texto_final, botones_finales)
                    print(f"[+] Informe enviado con {len(nuevos_hallazgos)} cargos.", flush=True)

            print("[*] Revisión sin novedades.", flush=True)
        except Exception as e:
            print(f"[-] Error: {e}", flush=True)
            
        time.sleep(900) # 15 min

if __name__ == "__main__":
    threading.Thread(target=monitorear, daemon=True).start()
    run_web_server()
