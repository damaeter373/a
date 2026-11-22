# PhoneMix

Escucha el audio de tu **teléfono Android** y el de tu **PC con Windows** en los **mismos audífonos** (los de la PC), con volumen independiente para cada uno.

```
 Teléfono (Android)                         PC (Windows)
 ┌────────────────────┐   Wi-Fi / USB /    ┌─────────────────────────┐
 │ PhoneMix Emisor    │ ── Bluetooth ───►  │ PhoneMix Receptor       │──► Audífonos de la PC
 │ captura su audio   │   (TCP)            │ mezcla con el juego     │     (juego + teléfono)
 └────────────────────┘                    └─────────────────────────┘
```

## Qué incluye

| Carpeta | Qué es |
|---|---|
| `windows/` | App receptora (Python + ventana). Reproduce el audio del teléfono y controla los volúmenes. |
| `android/` | App emisora (proyecto de Android Studio, Kotlin). Captura el audio interno y lo envía. |

## Funciones

- **Tres volúmenes independientes**: *Teléfono* (0–150 %), *PC / juegos y apps* (0–100 %) y *General* de Windows. Ejemplo: teléfono 60 %, PC 30 %.
- **Silenciar** el teléfono o la PC por separado.
- **Medidor de nivel** del audio del teléfono.
- **Latencia ajustable** (20–300 ms): menos = más inmediato, más = más estable.
- **Elegir la salida de audio** (por defecto, la predeterminada de Windows).
- **«No tocar estas apps»**: p. ej. `discord, spotify` para que su volumen no cambie.
- **Búsqueda automática de la PC** desde el teléfono («Buscar PC en la red»).
- **Reconexión automática** si se corta la conexión.
- Recuerda tus ajustes. Al cerrar, devuelve los volúmenes originales de tus apps.

## Instalación en Windows

1. Instala **Python 3.10 o superior** desde python.org (marca «Add Python to PATH»).
2. Abre la carpeta `windows` y haz doble clic en **`iniciar_phonemix.bat`**. La primera vez instala las dependencias (`requirements.txt`) y abre la app.
3. Si Windows pregunta por el Firewall, permite el acceso en **redes privadas**.

## Instalación en Android

Necesitas **Android 10 o superior** y **Android Studio** en la PC.

1. Abre Android Studio → *Open* → elige la carpeta `android`. Espera a que termine la sincronización de Gradle (descarga lo necesario).
2. En el teléfono activa *Opciones de desarrollador* → *Depuración USB*, conéctalo por USB y pulsa **Run ▶**. (O bien *Build → Build APK(s)* y pasa el APK al teléfono.)

## Uso

1. Abre **PhoneMix** en la PC.
2. Abre **PhoneMix** en el teléfono, conecta (ver abajo) y pulsa **Iniciar envío**.
3. Android pedirá permiso de audio y de captura. En el diálogo de captura elige **«Toda la pantalla»**; la app solo toma el audio, no la imagen.
4. Reproduce lo que quieras en el teléfono. Ajusta los volúmenes en la PC.

### Formas de conectar

| Medio | Cómo |
|---|---|
| **Wi-Fi** | Teléfono y PC en la misma red. Pulsa «Buscar PC en la red» o escribe la IP que muestra PhoneMix en la PC. |
| **USB (cable)** | Activa Depuración USB, instala *platform-tools* (adb) y en la PC pulsa **«Conectar por USB (adb)»** (o ejecuta `usb_conectar.bat`). En el teléfono usa la dirección **127.0.0.1**. No necesita Wi-Fi. Alternativa sin adb: activa *Anclaje USB* en el teléfono y usa la IP que PhoneMix muestre para esa red. |
| **Bluetooth** | Activa **Conexión Bluetooth (anclaje)** en el teléfono, vincula la PC y únela a esa red (Windows: *Dispositivos e impresoras* → clic derecho al teléfono → *Conectar mediante* → *Punto de acceso*). Usa la IP que PhoneMix muestre para esa red. Es un poco menos estable: sube la latencia a ~120–200 ms si hay cortes. |

> **Por qué el Bluetooth va así:** Windows normalmente no puede actuar como «bocina» Bluetooth para recibir audio de un teléfono, así que PhoneMix manda el audio por la red de anclaje Bluetooth en lugar de usar el perfil de audio.

## Cómo funcionan los volúmenes

- **Teléfono**: PhoneMix aplica la ganancia al audio que llega.
- **PC**: PhoneMix fija el volumen de cada app de Windows (juego, navegador…) al porcentaje elegido, en cuanto mueves ese control o pulsas *Silenciar*. Las apps nuevas se ajustan solas. «Soltar volumen de la PC» y cerrar PhoneMix restauran los volúmenes originales.
- **General**: volumen maestro de Windows (afecta a ambos).

Volumen final de cada fuente = (su control) × (General).

## Limitaciones

- Solo Android 10+; en iPhone no existe forma de capturar el audio interno.
- Algunas apps bloquean la captura de su audio (p. ej. Netflix y otras con DRM).
- Mientras captura, el teléfono sigue reproduciendo por su propio altavoz: baja el volumen del teléfono o silencia el altavoz y comprueba si el audio sigue llegando bien a la PC (en la mayoría de equipos la captura no depende de ese volumen).
- Android 14+ pide el permiso de captura cada vez que pulsas *Iniciar envío*.
- Latencia típica por Wi-Fi/USB: ~60–100 ms. Es perfecta para video, música o streams; no para juegos de ritmo en el teléfono.

## Problemas comunes

| Síntoma | Qué hacer |
|---|---|
| El teléfono no encuentra la PC | Revisa el Firewall (redes privadas) y que ambos estén en la misma red; escribe la IP a mano. |
| Cortes o crujidos | Sube «Latencia / estabilidad» en la PC. |
| No se oye nada pero dice «Conectado» | Confirma que la app del teléfono permite captura de audio y que no está pausada; mira si se mueve el medidor. |
| «No encontré adb» | Instala *SDK Platform-Tools* de Android y agrega la carpeta al PATH. |
| No controla el volumen de la PC | Falta `pycaw`: ejecuta `python -m pip install -r requirements.txt`. |
