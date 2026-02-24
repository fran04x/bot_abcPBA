import ssl
import requests
import urllib3
import time
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# CONFIGURACIÓN DEL BOT
# ==========================================
CUIL = "REMOVED"
PASSWORD = "REMOVED"

TELEGRAM_TOKEN = "REMOVED"
TELEGRAM_CHAT_ID = "REMOVED"
# ==========================================

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"[-] Error enviando Telegram: {e}", flush=True)

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = context
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

def iniciar_sesion():
    session = requests.Session()
    session.mount('https://', TLSAdapter())
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/x-www-form-urlencoded'
    })
    
    login_url = "https://login.abc.gob.ar/nidp/idff/sso?sid=2&sid=2"
    payload = {'option': 'credential', 'target': 'https://menu.abc.gob.ar/', 'Ecom_User_ID': CUIL, 'Ecom_Password': PASSWORD}

    try:
        session.post(login_url, data=payload, verify=False)
        if session.cookies.get_dict():
            return session
    except:
        pass
    return None

def buscar_cargos(session):
    url_solr = "https://servicios3.abc.gob.ar/valoracion.docente/api/apd.oferta.encabezado/select"
    parametros = {
        "q": 'descdistrito:"GENERAL PUEYRREDON" AND estado:"Publicada"',
        "rows": "1000",
        "wt": "json"
    }
    try:
        respuesta = session.get(url_solr, params=parametros, verify=False)
        if respuesta.status_code == 200:
            datos = respuesta.json()
            ofertas = datos.get("response", {}).get("docs", [])
            ofertas_encontradas = []
            for oferta in ofertas:
                cargo = str(oferta.get("cargo", "")).upper()
                jornada = str(oferta.get("jornada", "")).upper()
                if "MAESTRO DE GRADO" in cargo and jornada == "JC":
                    ofertas_encontradas.append(oferta)
            return ofertas_encontradas
    except:
        return []

if __name__ == "__main__":
    print("[*] Iniciando el bot en Render...", flush=True)
    enviar_telegram("✅ **Bot activado en la nube (Render).**\nMonitoreo 24/7 en marcha.")
    
    ofertas_avisadas = set() 
    
    while True:
        print("[*] Buscando ofertas nuevas...", flush=True)
        sesion_activa = iniciar_sesion()
        if sesion_activa:
            cargos_ideales = buscar_cargos(sesion_activa)
            for oferta in cargos_ideales:
                id_oferta = oferta.get("idoferta")
                if id_oferta not in ofertas_avisadas:
                    escuela = oferta.get('escuela', 'Desconocida')
                    direccion = oferta.get('domiciliodesempeno', 'Sin dirección')
                    toma = oferta.get('tomaposesion', 'Sin fecha')
                    
                    mensaje = f"🚨 **¡NUEVO CARGO DE 8 HORAS!** 🚨\n\n🏫 **Escuela:** {escuela}\n📍 **Dirección:** {direccion}\n📅 **Toma:** {toma[:10]}\n\n👉 [Postularse](https://misservicios.abc.gob.ar/actos.publicos.digitales/)"
                    enviar_telegram(mensaje)
                    ofertas_avisadas.add(id_oferta)
                    print(f"[+] Aviso enviado: {escuela}", flush=True)
        
        # Pausa de 30 minutos (1800 segundos)
        time.sleep(1800)
