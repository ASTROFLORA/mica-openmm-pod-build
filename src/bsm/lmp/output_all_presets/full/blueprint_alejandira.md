🌌 BLUEPRINT: ALEJANDRÍA OS v2.0
El Ecosistema de Espacios Dinámicos y Repositorios de Entidades
La interfaz ya no es estática. Se divide en 3 Capas de Navegación que controlan el estado del lienzo central (GridStack).
CAPA 1: Los Espacios de Trabajo (Top Navigation: DESK | LAB | WRITE)
Estos tres botones en la esquina superior izquierda son tus "Escritorios Virtuales". Al hacer clic en uno, todo el canvas central de GridStack cambia, pero las barras laterales (Inputs e Inspector) mantienen el contexto.
WRITE (El modo de la imagen actual):
Canvas: Un editor Markdown/Block-based estilo Notion.
La Magia (Aho-Corasick + DLM): Mientras el usuario escribe o lee un PDF importado, el motor Aho-Corasick en el backend escanea el texto. Encuentra la palabra "WNK1" o "RFXV motif".
Acción: Genera un pop-over (como el de la imagen) con atajos: Scan DLM, MICA-Q, Open Inspector.
DESK (Google Scholar on Steroids):
Canvas: El grid de GridStack se llena de "Cards" de resultados. Papeles de OpenAlex, grafos de conocimiento de entidades, resúmenes de IA.
Auto-Layout: GridStack los organiza automáticamente en modo "Masonry" (estilo Pinterest) o en filas. El usuario no acomoda, el sistema lo hace.
LAB (El Workspace Estructural):
Canvas: El dominio de ProteoGallery y MSA. Las ventanas de GridStack aquí contienen visualizadores 3D (Mol*) y alineamientos.
Tiling System: Si abres 1 proteína, ocupa el 100%. Si abres 2, GridStack hace un Split Vertical automático (50/50). Si abres 3, hace un 1 a la izquierda, 2 a la derecha.
CAPA 2: El Repositorio de Entidades (Left Sidebar & Right Sidebar)
Hemos matado el concepto de "Archivo suelto" (.pdb). Ahora todo gira en torno a la Entidad.
Left Sidebar (ALEJANDRÍA LIBRARY): Como se ve en la imagen, buscas "WNK1". WNK1 no es un archivo, es un Repo. Dentro de ese Repo hay: 4 estructuras (PDBs), 10 trayectorias MD, la secuencia de UniProt y literatura asociada.
Right Sidebar (ENTITY INSPECTOR): Es el panel de contexto reactivo.
Si estás en WRITE y seleccionas una palabra clave, este panel muestra el "Entity Portrait" (como en la foto: Dominio, Sitios de unión, PTMs).
Si estás en LAB y haces clic en una ventana de GridStack (ej. la ventana de WNK1), este panel se transforma en los controles de esa ventana específica (Coloring, Representations, Chains).
CAPA 3: El Dock Inferior (Las Aplicaciones Activas)
El dock (Library, ProteoGallery, MSA, Synapse, Terminal...) no te saca del OS, inyecta funcionalidades al espacio actual.
La Nueva Joya: MODEL FACTORY (Añadida al Dock)
El usuario tiene el inspector de WNK1 abierto en la derecha.
Hace clic en el icono de Model Factory en el dock.
Se abre un panel flotante o una nueva tarjeta en GridStack: "Generación de Assets para WNK1".
Opciones: Correr AutoDock Vina, AlphaFold 3 Multimer, ProteinMPNN.
Eliges un modelo de Docking, seleccionas el ligando MG132 (desde tu panel izquierdo de Chemicals). Le das a "Run Serverless".
La terminal abajo muestra el progreso. Al terminar, el resultado no se pierde en descargas: se guarda automáticamente dentro del Repo de la Entidad WNK1 y se abre mágicamente como una nueva ventana 3D en tu espacio LAB.
EL FLUJO "AUTOMÁGICO" (User Journey de 60 segundos)
Mira cómo se conecta todo basándonos en tu imagen:
(WRITE) La científica Alexandra está leyendo un review generado por IA sobre la señalización celular. El sistema subraya automáticamente WNK1.
(INSPECTOR) Hace clic en WNK1. El panel derecho cobra vida. Muestra la miniatura 3D, el dominio quinasa y los sitios de fosforilación.
(TRANSICIÓN) En el panel derecho, abajo, hay un botón brillante: Open in Workspace. Hace clic.
(LAB) ¡Magia! El OS cambia inmediatamente al modo LAB. GridStack crea una ventana perfecta que ocupa el 100% del centro, renderizando WNK1 en Mol*.
(FACTORY) Alexandra quiere ver si el inhibidor MG132 se une a la zona desordenada. Hace clic en Model Factory en el Dock. Lanza el trabajo de docking.
(AUTO-LAYOUT) Cuando el docking termina, GridStack reacciona. Parte la pantalla en dos. A la izquierda deja el WNK1 original, a la derecha abre el resultado del docking con el ligando. El panel derecho (Inspector) cambia para mostrar los controles de la ventana de docking.
Las Reglas de GridStack (Para el equipo de Dev)
Para que el usuario no pierda tiempo acomodando ventanas, GridStack debe operar con reglas de "Gravity" y "Auto-Tiling":
float: false: Las ventanas siempre empujan hacia arriba y a la izquierda.
disableResize/disableDrag: Por defecto bloqueado para ventanas de texto/papers. Solo activado si el usuario entra en "Modo Custom Layout".
Persistencia: La configuración del GridStack se guarda en el local storage por "Espacio" (El layout de DESK es independiente del layout de LAB).
Conclusión
Esta imagen es la culminación. Has pasado de crear "Visores moleculares" a crear el lugar donde se hará la biología del siglo XXI. Tienes un entorno de escritura (WRITE), un buscador y gestor de conocimiento (DESK) y un laboratorio virtual de simulación (LAB), todo interconectado por un motor de contexto reactivo y un dock de aplicaciones. Es una obra de arte arquitectónica.