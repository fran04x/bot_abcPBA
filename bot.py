import os
import ssl
import requests
import urllib3
import time
import threading
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
        self.wfile.write(b"Buscador de ofertas activado.")

def run_web_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

# --- ENVÍO DE MENSAJE CON LINKS AZULES ---
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True # Evita que se llenen de vistas previas de la web
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
    print("[*] Monitoreo con diseño de links iniciado...", flush=True)
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
                nuevos = []
                
                for oferta in docs:
                    cargo = str(oferta.get("cargo", "")).upper()
                    jornada = str(oferta.get("jornada", "")).upper()
                    if "MAESTRO DE GRADO" in cargo and jornada == "JC":
                        id_o = oferta.get("idoferta")
                        if id_o not in ofertas_avisadas:
                            nuevos.append(oferta)
                            ofertas_avisadas.add(id_o)

                if nuevos:
                    # Armando el mensaje estético
                    cuerpo_mensaje = "🚨 **¡NUEVOS CARGOS DETECTADOS!** 🚨\n\n"
                    ts = int(time.time() * 1000)
                    
                    for cargo_info in nuevos:
                        escuela = cargo_info.get('escuela', 'N/A')
                        area = cargo_info.get('cargo', 'N/A')
                        curso = cargo_info.get('curso', 'N/A')
                        div = cargo_info.get('division', 'N/A')
                        id_o = cargo_info.get('idoferta')
                        id_d = cargo_info.get('iddetalle') or id_o
                        
                        link = f"https://misservicios.abc.gob.ar/actos.publicos.digitales/postulantes/?oferta={id_o}&detalle={id_d}&_t={ts}"
                        
                        # Bloque de información
                        cuerpo_mensaje += f"🏫 **Escuela:** {escuela}\n"
                        cuerpo_mensaje += f"📚 **Área:** `{area}`\n"
                        cuerpo_mensaje += f"👥 **Curso/Div:** {curso} - {div}\n"
                        cuerpo_mensaje += f"🔗 [CLIC AQUÍ PARA POSTULARSE]({link})\n"
                        cuerpo_mensaje += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                    
                    enviar_telegram(cuerpo_mensaje)
                    print(f"[+] Informe enviado ({len(nuevos)} cargos).", flush=True)

            print("[*] Sin novedades en esta vuelta.", flush=True)
        except Exception as e:
            print(f"[-] Error: {e}", flush=True)
            
        time.sleep(900)

if __name__ == "__main__":
    threading.Thread(target=monitorear, daemon=True).start()
    run_web_server()
