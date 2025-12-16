import logging
import time
import random
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Playwright instance global
playwright_instance = None
browser_instance = None

def get_browser():
    global playwright_instance, browser_instance
    
    if browser_instance is None or not browser_instance.is_connected():
        logger.info("🚀 Iniciando Playwright con stealth...")
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
            ]
        )
    return browser_instance

# Pre-calentar
try:
    get_browser()
except Exception as e:
    logger.error(f"Error pre-calentando: {e}")

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "method": "Playwright Stealth"}), 200

@app.route('/consulta_cedula', methods=['POST'])
def consulta_cedula_api():
    start_time = time.time()
    context = None
    page = None
    
    try:
        data = request.json
        cedula = str(data.get('cedula', '')).strip()
        
        if not cedula or not cedula.isdigit():
            return jsonify({"status": "error", "mensaje": "Cédula inválida"}), 400
        
        logger.info(f"📋 Consultando: {cedula}")
        
        browser = get_browser()
        
        # Context con stealth
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
        
        # Inyectar scripts anti-detección
        context.add_init_script("""
            // Eliminar webdriver
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            
            // Sobrescribir plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            
            // Sobrescribir languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['es-CO', 'es', 'en']
            });
            
            // Chrome runtime
            window.chrome = {runtime: {}};
            
            // Permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({state: Notification.permission}) :
                    originalQuery(parameters)
            );
        """)
        
        page = context.new_page()
        
        # Bloquear recursos innecesarios
        page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2,ttf,ico}", lambda route: route.abort())
        
        logger.info("🌐 Navegando...")
        
        # Navegar con delay humano
        time.sleep(random.uniform(1, 2))
        
        page.goto(
            'https://wsp.registraduria.gov.co/censo/consultar',
            wait_until='domcontentloaded',
            timeout=30000
        )
        
        # Esperar formulario
        page.wait_for_selector('input[name="numdoc"]', state='visible', timeout=10000)
        
        # Delay humano antes de llenar
        time.sleep(random.uniform(1, 2))
        
        # Llenar con delays
        page.type('input[name="numdoc"]', cedula, delay=random.randint(50, 150))
        
        # Delay antes de submit
        time.sleep(random.uniform(1, 2))
        
        # Submit
        logger.info("📤 Enviando...")
        try:
            with page.expect_navigation(wait_until='domcontentloaded', timeout=40000):
                page.click('input[type="submit"]')
        except PlaywrightTimeoutError:
            logger.warning("Timeout en navegación, pero continuando...")
        
        time.sleep(2)
        
        # Obtener contenido
        html = page.content()
        texto = page.inner_text('body')
        
        total_time = time.time() - start_time
        logger.info(f"✅ Completado en {total_time:.2f}s")
        
        # Análisis
        texto_lower = texto.lower()
        html_lower = html.lower()
        
        if any(word in html_lower for word in ['captcha', 'recaptcha', 'robot']):
            return jsonify({
                "status": "captcha",
                "mensaje": "CAPTCHA detectado",
                "cedula": cedula,
                "tiempo_proceso": round(total_time, 2)
            }), 200
        
        if any(phrase in texto_lower for phrase in ['no se encontró', 'no existe']):
            return jsonify({
                "status": "not_found",
                "mensaje": "Cédula no encontrada",
                "cedula": cedula,
                "tiempo_proceso": round(total_time, 2)
            }), 200
        
        return jsonify({
            "status": "success",
            "cedula": cedula,
            "resultado_bruto": texto,
            "tiempo_proceso": round(total_time, 2)
        }), 200
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
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
        "servicio": "Registraduría - Playwright Stealth",
        "version": "4.0",
        "features": ["Anti-detección", "Scripts stealth", "Delays humanos"]
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)
