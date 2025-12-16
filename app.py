import logging
import time
import random
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

playwright_instance = None
browser_instance = None

def get_browser():
    global playwright_instance, browser_instance
    
    if browser_instance is None or not browser_instance.is_connected():
        logger.info("Iniciando Playwright con stealth...")
        if playwright_instance is None:
            playwright_instance = sync_playwright().start()
        
        browser_instance = playwright_instance.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-site-isolation-trials',
            ]
        )
    return browser_instance

try:
    get_browser()
    logger.info("Browser pre-calentado exitosamente")
except Exception as e:
    logger.error(f"Error pre-calentando browser: {e}")

@app.route('/health', methods=['GET'])
def health_check():
    try:
        browser_status = "connected" if browser_instance and browser_instance.is_connected() else "disconnected"
        return jsonify({
            "status": "healthy",
            "method": "Playwright Stealth",
            "browser": browser_status,
            "timestamp": time.time()
        }), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

@app.route('/consulta_cedula', methods=['POST'])
def consulta_cedula_api():
    start_time = time.time()
    context = None
    page = None
    
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "mensaje": "No se envio JSON"}), 400
        
        cedula = data.get('cedula')
        if not cedula:
            return jsonify({"status": "error", "mensaje": "Falta cedula"}), 400
        
        cedula_str = str(cedula).strip()
        if not cedula_str.isdigit():
            return jsonify({"status": "error", "mensaje": "Cedula debe ser numerica"}), 400
        
        if len(cedula_str) < 6 or len(cedula_str) > 10:
            return jsonify({"status": "error", "mensaje": "Cedula debe tener entre 6 y 10 digitos"}), 400
        
        logger.info(f"Consultando cedula: {cedula_str}")
        
        browser = get_browser()
        
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='es-CO',
            timezone_id='America/Bogota',
            viewport={'width': 1920, 'height': 1080},
            java_script_enabled=True,
            extra_http_headers={
                'Accept-Language': 'es-CO,es;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }
        )
        
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-CO', 'es', 'en']});
            window.chrome = {runtime: {}};
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({state: Notification.permission}) :
                    originalQuery(parameters)
            );
        """)
        
        page = context.new_page()
        
        page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2,ttf,ico}", lambda route: route.abort())
        
        logger.info("Navegando a Registraduria...")
        
        time.sleep(random.uniform(1, 2))
        
        page.goto(
            'https://wsp.registraduria.gov.co/censo/consultar',
            wait_until='domcontentloaded',
            timeout=30000
        )
        
        page.wait_for_selector('input[name="numdoc"]', state='visible', timeout=10000)
        
        time.sleep(random.uniform(1, 2))
        
        page.type('input[name="numdoc"]', cedula_str, delay=random.randint(50, 150))
        
        time.sleep(random.uniform(1, 2))
        
        logger.info("Enviando formulario...")
        try:
            with page.expect_navigation(wait_until='domcontentloaded', timeout=40000):
                page.click('input[type="submit"]')
        except PlaywrightTimeoutError:
            logger.warning("Timeout en navegacion, continuando...")
        
        time.sleep(2)
        
        html = page.content()
        texto = page.inner_text('body')
        
        total_time = time.time() - start_time
        logger.info(f"Completado en {total_time:.2f}s")
        
        texto_lower = texto.lower()
        html_lower = html.lower()
        
        if any(word in html_lower for word in ['captcha', 'recaptcha', 'robot', 'g-recaptcha', 'hcaptcha']):
            logger.warning("CAPTCHA detectado")
            return jsonify({
                "status": "captcha",
                "mensaje": "CAPTCHA detectado",
                "cedula": cedula_str,
                "tiempo_proceso": round(total_time, 2)
            }), 200
        
        if any(phrase in texto_lower for phrase in ['no se encontro', 'no existe', 'no hay']):
            return jsonify({
                "status": "not_found",
                "mensaje": "Cedula no encontrada",
                "cedula": cedula_str,
                "tiempo_proceso": round(total_time, 2)
            }), 200
        
        resultado = {
            "nombre": None,
            "cedula": cedula_str,
            "puesto_votacion": None,
            "direccion": None,
            "municipio": None,
            "departamento": None,
            "mesa": None
        }
        
        try:
            lineas = texto.split('\n')
            for i, linea in enumerate(lineas):
                linea_lower = linea.lower().strip()
                if 'nombre' in linea_lower and i + 1 < len(lineas):
                    resultado['nombre'] = lineas[i + 1].strip()
                elif 'puesto' in linea_lower and i + 1 < len(lineas):
                    resultado['puesto_votacion'] = lineas[i + 1].strip()
                elif 'direccion' in linea_lower and i + 1 < len(lineas):
                    resultado['direccion'] = lineas[i + 1].strip()
                elif 'municipio' in linea_lower and i + 1 < len(lineas):
                    resultado['municipio'] = lineas[i + 1].strip()
                elif 'departamento' in linea_lower and i + 1 < len(lineas):
                    resultado['departamento'] = lineas[i + 1].strip()
                elif 'mesa' in linea_lower and i + 1 < len(lineas):
                    resultado['mesa'] = lineas[i + 1].strip()
        except Exception as e:
            logger.warning(f"Error parseando: {e}")
        
        return jsonify({
            "status": "success",
            "cedula": cedula_str,
            "datos_estructurados": resultado,
            "resultado_bruto": texto,
            "tiempo_proceso": round(total_time, 2)
        }), 200
        
    except PlaywrightTimeoutError:
        total_time = time.time() - start_time
        logger.error("Timeout en Playwright")
        return jsonify({
            "status": "error",
            "mensaje": "Timeout al consultar",
            "error_type": "timeout",
            "tiempo_transcurrido": round(total_time, 2)
        }), 504
        
    except Exception as e:
        total_time = time.time() - start_time
        logger.error(f"Error: {str(e)}", exc_info=True)
        return jsonify({
            "status": "error",
            "mensaje": "Error interno",
            "error_type": "server_error",
            "error_detail": str(e),
            "tiempo_transcurrido": round(total_time, 2)
        }), 500
    
    finally:
        if page:
            try:
                page.close()
            except:
                pass
        if context:
            try:
                context.close()
            except:
                pass

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "servicio": "Registraduria - Playwright Stealth",
        "version": "4.0",
        "features": [
            "Anti-deteccion avanzada",
            "Scripts stealth inyectados",
            "Delays humanos aleatorios",
            "Bloqueo de recursos innecesarios"
        ],
        "endpoints": {
            "health": "GET /health",
            "consulta": "POST /consulta_cedula"
        },
        "ejemplo": {
            "method": "POST",
            "url": "/consulta_cedula",
            "body": {"cedula": "12345678"}
        }
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)
