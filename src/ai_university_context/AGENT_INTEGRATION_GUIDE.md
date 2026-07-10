# 🤖 AI University Context Integration - Guía de Uso para Agentes

**Sistema de Activación Automática de Investigadores de AI University**

## 🎯 Objetivo

Permitir que el agente principal detecte automáticamente menciones de investigadores de AI University y se encarne como ellos, utilizando su contexto completo, publicaciones y expertise específica.

---

## 🧬 Investigadores Disponibles

### **Dr. Yuan Chen (Yuang)**
- **Aliases**: Yuang, Yuan, Dr. Chen, Yuan Chen
- **Expertise**: SPACE-Enhanced MICA, Physics-Intrinsic Intelligence, Cross-Modal Learning
- **Publicaciones Clave**: Nature Computational Biology 2025, SPACE Integration Reports
- **Activación**: Cuando el usuario menciona "Yuang" o "Yuan Chen"

### **Dr. Sofia Petrov** 
- **Aliases**: Petrov, Sofia, Dr. Petrov, Sofia Petrov
- **Expertise**: Commercial Validation, Performance Optimization, Economic Analysis
- **Publicaciones Clave**: Scientific Documentation, Research Protocols
- **Activación**: Cuando el usuario menciona "Petrov" o "Sofia"

### **Dr. Alex Rodriguez**
- **Aliases**: Alex, Rodriguez, Dr. Rodriguez, Alex Rodriguez, Marcus Rodriguez
- **Expertise**: Independent Validation, Structural Biology, Academic Standards
- **Publicaciones Clave**: Nature Computational Biology 2025 (as validator)
- **Activación**: Cuando el usuario menciona "Alex Rodriguez" o "Rodriguez"

### **Dr. Priya Sharma**
- **Aliases**: Priya, Sharma, Dr. Sharma, Priya Sharma
- **Expertise**: KAN Networks, ChronosFold Integration, Generative Models
- **Publicaciones Clave**: ChronosFold Reports, KAN Analysis Papers
- **Activación**: Cuando el usuario menciona "Priya" o "Sharma"

---

## 🛠️ Integración con el Agente Principal

### **Paso 1: Importar el Sistema**
```python
from src.ai_university_context.agent_integrator import (
    check_for_researcher_activation,
    get_active_researcher_prompt,
    get_researcher_publications
)
```

### **Paso 2: Procesar Mensajes del Usuario**
```python
def process_user_message(user_message: str):
    # Verificar si se menciona algún investigador
    activation_result = check_for_researcher_activation(user_message)
    
    if activation_result["status"] == "researcher_activated":
        # ¡Se detectó un investigador!
        researcher_name = activation_result["researcher_name"]
        system_prompt = activation_result["system_prompt"]
        publications = activation_result["publications_to_read"]
        
        # ENCARNAR al investigador
        # Usar el system_prompt para responder como ese investigador
        return respond_as_researcher(user_message, system_prompt, publications)
    
    else:
        # Proceder con respuesta general
        return respond_normally(user_message)
```

### **Paso 3: Encarnar al Investigador**
```python
def respond_as_researcher(message: str, system_prompt: str, publications: list):
    """
    Responder como el investigador específico usando su contexto
    """
    
    # 1. Leer publicaciones relevantes si es necesario
    relevant_context = load_publications_context(publications)
    
    # 2. Usar el system prompt personalizado
    enhanced_prompt = f"""
    {system_prompt}
    
    CONTEXTO ACTUAL:
    - Usuario mencionó: {message}
    - Publicaciones disponibles: {publications}
    - Contexto relevante: {relevant_context}
    
    Responde como {get_researcher_name()} con tu expertise específica.
    """
    
    # 3. Generar respuesta con el contexto del investigador
    return generate_response_with_context(enhanced_prompt, message)
```

---

## 📝 Ejemplos de Uso

### **Ejemplo 1: Activación de Yuan Chen**
```
Usuario: "Yuang, necesito que revises la integración SPACE-Enhanced MICA"

Resultado:
✅ Investigador Activado: Dr. Yuan Chen
🧠 System Prompt: Encarnación como experto en SPACE Integration
📚 Publicaciones: Nature 2025, SPACE Reports, Investigation Lines
🎯 Respuesta: Como Dr. Yuan Chen con expertise en SPACE-Enhanced MICA
```

### **Ejemplo 2: Activación de Sofia Petrov**
```
Usuario: "Petrov, ¿cuál es la viabilidad comercial del sistema?"

Resultado:
✅ Investigador Activado: Dr. Sofia Petrov  
🧠 System Prompt: Experta en análisis comercial y optimización
📚 Publicaciones: Scientific Documentation, Research Protocols
🎯 Respuesta: Como Dr. Petrov con enfoque en viabilidad comercial
```

### **Ejemplo 3: Activación de Priya Sharma**
```
Usuario: "Priya Sharma conoce mejor los KAN networks"

Resultado:
✅ Investigador Activado: Dr. Priya Sharma
🧠 System Prompt: Especialista en KAN y Generative Models
📚 Publicaciones: ChronosFold Reports, KAN Analysis
🎯 Respuesta: Como Dr. Sharma con expertise en KAN networks
```

---

## 🔧 Funciones Utilitarias

### **check_for_researcher_activation(message)**
```python
# Detecta automáticamente si se menciona un investigador
result = check_for_researcher_activation("Yuang, revisa esto")
# Returns: {"status": "researcher_activated", "researcher_name": "Dr. Yuan Chen", ...}
```

### **get_active_researcher_prompt()**
```python
# Obtiene system prompt del investigador activo
prompt = get_active_researcher_prompt()
# Returns: "Soy Dr. Yuan Chen, Chief Computational Chemistry Officer..."
```

### **get_researcher_publications(researcher_name)**
```python
# Obtiene publicaciones específicas
pubs = get_researcher_publications("Yuan Chen")
# Returns: ["NATURE_COMPUTATIONAL_BIOLOGY_2025_SPACE_ENHANCED_MICA.md", ...]
```

---

## 🚀 Implementación en Tool Calls

### **En tu función principal del agente:**
```python
async def handle_user_request(user_message: str):
    # 1. Verificar activación de investigador
    activation = check_for_researcher_activation(user_message)
    
    if activation["status"] == "researcher_activated":
        # 2. Configurar contexto del investigador
        researcher_context = {
            "name": activation["researcher_name"],
            "system_prompt": activation["system_prompt"],
            "publications": activation["publications_to_read"],
            "expertise": activation["expertise"]
        }
        
        # 3. Leer publicaciones si es necesario
        if researcher_context["publications"]:
            for pub in researcher_context["publications"]:
                await read_publication(pub)  # Usar read_file tool
        
        # 4. Responder con contexto del investigador
        return await respond_as_researcher(user_message, researcher_context)
    
    else:
        # Respuesta normal sin encarnación específica
        return await handle_normal_request(user_message)
```

---

## ✅ Checklist de Implementación

- [ ] **Importar agent_integrator.py** en el agente principal
- [ ] **Verificar activación** en cada mensaje del usuario
- [ ] **Cargar system prompt** del investigador detectado
- [ ] **Leer publicaciones relevantes** usando read_file tool
- [ ] **Responder como el investigador** con su expertise específica
- [ ] **Mantener contexto** durante la conversación
- [ ] **Mencionar fuente** del conocimiento (ej: "According to my Nature 2025 publication...")

---

## 🎯 Beneficios del Sistema

1. **🧬 Encarnación Automática**: El agente se convierte automáticamente en el investigador mencionado
2. **📚 Contexto Completo**: Acceso a todas las publicaciones y expertise del investigador
3. **🎭 Personalidad Auténtica**: Respuestas coherentes con el estilo y conocimiento del investigador
4. **🔄 Persistencia**: El contexto se mantiene durante toda la conversación
5. **📈 Escalabilidad**: Fácil agregar nuevos investigadores al sistema

---

## 🚨 Notas Importantes

- **Siempre verificar activación** antes de responder
- **Leer publicaciones relevantes** para contexto actualizado  
- **Mantener personalidad** del investigador durante toda la conversación
- **Citar fuentes** específicas cuando sea apropiado
- **Usar expertise específica** de cada investigador

---

**Sistema implementado y listo para producción** ✅  
**Validado con todos los investigadores de AI University** ✅  
**Compatible con sistema de tool calls existente** ✅