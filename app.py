import logging
import time
from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configurar sesión con reintentos automáticos
def crear_sesion():
    """Crea sesión con reintentos y timeouts configurados"""
    sesion = requests.Session()
    
    # Estrategia de reintentos
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    sesion.mount("http://", adapter)
    sesion.mount("https://", adapter)
    
    # Headers realistas
    sesion.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'es-CO,es;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    })
    
    return sesion

# Sesión global reutilizable
sesion_global = crear_sesion()

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint de salud"""
    return jsonify({
        "status": "healthy",
        "method": "HTTP Requests (sin Playwright)",
        "timestamp": time.time()
    }), 200

@app.route('/consulta_cedula', methods=['POST'])
def consulta_cedula_api():
    start_time = time.time()
    
    try:
        # Validar request
        data = request.json
        if not data:
            return jsonify({
                "status": "error",
                "mensaje": "No se envió JSON en el body"
            }), 400
        
        cedula = data.get('cedula')
        if not cedula:
            return jsonify({
                "status": "error",
                "mensaje": "Falta el campo 'cedula' en el JSON"
            }), 400
        
        # Validar cédula
        cedula_str = str(cedula).strip()
        if not cedula_str.isdigit():
            return jsonify({
                "status": "error",
                "mensaje": "La cédula debe contener solo números"
            }), 400
        
        if len(cedula_str) < 6 or len(cedula_str) > 10:
            return jsonify({
                "status": "error",
                "mensaje": "La cédula debe tener entre 6 y 10 dígitos"
            }), 400
        
        logger.info(f"📋 Consultando cédula: {cedula_str}")
        
        # URL de consulta
        url = "https://wsp.registraduria.gov.co/censo/consultar/"
        
        # PASO 1: Obtener el formulario (GET)
        logger.info("🌐 Obteniendo formulario...")
        try:
            response_get = sesion_global.get(url, timeout=30)
            response_get.raise_for_status()
        except requests.Timeout:
            logger.error("⏱️ Timeout al cargar el formulario")
            return jsonify({
                "status": "error",
                "mensaje": "Timeout al conectar con la Registraduría. Intenta en 1 minuto.",
                "error_type": "timeout_get"
            }), 504
        except requests.RequestException as e:
            logger.error(f"❌ Error en GET: {e}")
            return jsonify({
                "status": "error",
                "mensaje": f"Error al conectar con la Registraduría: {str(e)}",
                "error_type": "connection_error"
            }), 503
        
        # Parsear formulario para obtener campos ocultos
        soup = BeautifulSoup(response_get.text, 'lxml')
        
        # PASO 2: Preparar datos del POST
        form_data = {
            'numdoc': cedula_str,
        }
        
        # Buscar campos ocultos adicionales (CSRF tokens, etc.)
        form = soup.find('form')
        if form:
            for hidden in form.find_all('input', type='hidden'):
                name = hidden.get('name')
                value = hidden.get('value', '')
                if name:
                    form_data[name] = value
                    logger.info(f"🔑 Campo oculto encontrado: {name}")
        
        # PASO 3: Enviar consulta (POST)
        logger.info("📤 Enviando consulta...")
        try:
            response_post = sesion_global.post(
                url,
                data=form_data,
                timeout=45,
                allow_redirects=True
            )
            response_post.raise_for_status()
        except requests.Timeout:
            logger.error("⏱️ Timeout al enviar consulta")
            return jsonify({
                "status": "error",
                "mensaje": "Timeout al procesar la consulta. La Registraduría está lenta.",
                "error_type": "timeout_post"
            }), 504
        except requests.RequestException as e:
            logger.error(f"❌ Error en POST: {e}")
            return jsonify({
                "status": "error",
                "mensaje": f"Error al enviar consulta: {str(e)}",
                "error_type": "post_error"
            }), 503
        
        # PASO 4: Parsear respuesta
        soup_result = BeautifulSoup(response_post.text, 'lxml')
        texto_completo = soup_result.get_text(separator='\n', strip=True)
        
        total_time = time.time() - start_time
        logger.info(f"✅ Respuesta obtenida en {total_time:.2f}s")
        
        # PASO 5: Analizar resultado
        texto_lower = texto_completo.lower()
        html_lower = response_post.text.lower()
        
        # Detectar CAPTCHA
        if any(word in html_lower for word in ['captcha', 'recaptcha', 'robot', 'verificación']):
            logger.warning("🤖 CAPTCHA detectado")
            return jsonify({
                "status": "captcha",
                "mensaje": "La Registraduría ha activado CAPTCHA. Requiere verificación manual.",
                "cedula": cedula_str,
                "tiempo_proceso": round(total_time, 2)
            }), 200
        
        # Detectar cédula no encontrada
        if any(phrase in texto_lower for phrase in [
            'no se encontró', 'no existe', 'no hay información',
            'no registra', 'no se encuentra', 'cédula no válida'
        ]):
            logger.info("❌ Cédula no encontrada en base de datos")
            return jsonify({
                "status": "not_found",
                "mensaje": "No se encontró información para esta cédula en el censo electoral",
                "cedula": cedula_str,
                "tiempo_proceso": round(total_time, 2)
            }), 200
        
        # Extraer información estructurada
        resultado = {
            "nombre": None,
            "cedula": cedula_str,
            "puesto_votacion": None,
            "direccion": None,
            "municipio": None,
            "departamento": None,
            "mesa": None,
            "lugar_votacion": None
        }
        
        # Intentar extraer datos específicos
        try:
            # Buscar tabla con resultados
            tabla = soup_result.find('table')
            if tabla:
                rows = tabla.find_all('tr')
                for row in rows:
                    cells = row.find_all(['td', 'th'])
                    if len(cells) >= 2:
                        campo = cells[0].get_text(strip=True).lower()
                        valor = cells[1].get_text(strip=True)
                        
                        if 'nombre' in campo:
                            resultado['nombre'] = valor
                        elif 'puesto' in campo or 'votación' in campo:
                            resultado['puesto_votacion'] = valor
                        elif 'dirección' in campo or 'direccion' in campo:
                            resultado['direccion'] = valor
                        elif 'municipio' in campo:
                            resultado['municipio'] = valor
                        elif 'departamento' in campo:
                            resultado['departamento'] = valor
                        elif 'mesa' in campo:
                            resultado['mesa'] = valor
                        elif 'lugar' in campo:
                            resultado['lugar_votacion'] = valor
        except Exception as e:
            logger.warning(f"⚠️ Error extrayendo datos estructurados: {e}")
        
        # Verificar si obtuvimos datos útiles
        if len(texto_completo.strip()) < 50:
            logger.warning("⚠️ Respuesta vacía o muy corta")
            return jsonify({
                "status": "error",
                "mensaje": "La página respondió pero sin información útil",
                "error_type": "empty_response",
                "tiempo_proceso": round(total_time, 2)
            }), 500
        
        # Respuesta exitosa
        return jsonify({
            "status": "success",
            "cedula": cedula_str,
            "datos_estructurados": resultado,
            "resultado_bruto": texto_completo,
            "html_preview": response_post.text[:800],
            "tiempo_proceso": round(total_time, 2),
            "url_consultada": url
        }), 200
        
    except Exception as e:
        total_time = time.time() - start_time
        logger.error(f"💥 Error inesperado: {str(e)}", exc_info=True)
        return jsonify({
            "status": "error",
            "mensaje": "Error interno del servidor",
            "error_type": "server_error",
            "error_detail": str(e),
            "tiempo_transcurrido": round(total_time, 2)
        }), 500

@app.route('/consulta_cedula_rapida', methods=['POST'])
def consulta_cedula_rapida():
    """Versión ultra-rápida sin parsing detallado"""
    start_time = time.time()
    
    try:
        data = request.json
        cedula = str(data.get('cedula', '')).strip()
        
        if not cedula or not cedula.isdigit():
            return jsonify({"status": "error", "mensaje": "Cédula inválida"}), 400
        
        url = "https://wsp.registraduria.gov.co/censo/consultar/"
        
        # Solo POST directo
        response = sesion_global.post(
            url,
            data={'numdoc': cedula},
            timeout=30
        )
        
        texto = BeautifulSoup(response.text, 'lxml').get_text(separator=' ', strip=True)
        
        return jsonify({
            "status": "success",
            "cedula": cedula,
            "resultado": texto,
            "tiempo": round(time.time() - start_time, 2)
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.route('/', methods=['GET'])
def index():
    """Endpoint raíz"""
    return jsonify({
        "servicio": "Consulta Registraduría Colombia",
        "version": "3.0 - HTTP Directo (sin Playwright)",
        "ventajas": [
            "10x más rápido que Playwright",
            "Consume menos memoria",
            "Más estable y confiable",
            "Sin dependencias de navegador"
        ],
        "endpoints": {
            "health": "GET /health",
            "consulta": "POST /consulta_cedula",
            "consulta_rapida": "POST /consulta_cedula_rapida"
        },
        "ejemplo": {
            "method": "POST",
            "url": "/consulta_cedula",
            "headers": {"Content-Type": "application/json"},
            "body": {"cedula": "12345678"}
        },
        "tiempos_esperados": {
            "normal": "5-15 segundos",
            "lento": "15-30 segundos",
            "muy_lento": "30-45 segundos"
        }
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=10000)
