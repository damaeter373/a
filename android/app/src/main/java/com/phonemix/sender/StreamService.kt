package com.phonemix.sender

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Captura el audio que reproduce el teléfono (AudioPlaybackCapture) y lo envía
 * por TCP a la app PhoneMix de Windows. Se reconecta sola si se corta la conexión.
 */
class StreamService : Service() {

    companion object {
        const val ACTION_START = "com.phonemix.sender.START"
        const val ACTION_STOP = "com.phonemix.sender.STOP"
        const val ACTION_STATUS = "com.phonemix.sender.STATUS"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_PROJECTION_DATA = "projection_data"
        const val EXTRA_HOST = "host"
        const val EXTRA_PORT = "port"
        const val EXTRA_STATUS = "status"

        const val SAMPLE_RATE = 48000
        private const val CHUNK_FRAMES = 480          // 10 ms
        private const val FRAME_BYTES = 4             // 16 bits x 2 canales
        private const val CHANNEL_ID = "phonemix_stream"
        private const val NOTIFICATION_ID = 1

        @Volatile var running = false
        @Volatile var lastStatus = "Detenido"
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val queue = LinkedBlockingQueue<ByteArray>(40) // máx. ~400 ms en cola

    private var projection: MediaProjection? = null
    private var projectionCallback: MediaProjection.Callback? = null
    private var record: AudioRecord? = null
    private var captureThread: Thread? = null
    private var senderThread: Thread? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var cpuLock: PowerManager.WakeLock? = null

    @Volatile private var stopFlag = false
    private var active = false
    private var host = "127.0.0.1"
    private var port = 48000

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> if (!active) startStreaming(intent)
            ACTION_STOP -> {
                stopEverything("Detenido")
                stopSelf()
            }
            else -> stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopEverything("Detenido")
        super.onDestroy()
    }

    // ------------------------------------------------------------------ //

    @Suppress("DEPRECATION")
    private fun startStreaming(intent: Intent) {
        host = intent.getStringExtra(EXTRA_HOST) ?: "127.0.0.1"
        port = intent.getIntExtra(EXTRA_PORT, 48000)
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
        val data: Intent? = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(EXTRA_PROJECTION_DATA, Intent::class.java)
        } else {
            intent.getParcelableExtra(EXTRA_PROJECTION_DATA)
        }

        createChannel()
        // Debe llamarse ANTES de getMediaProjection() (obligatorio desde Android 14).
        startForeground(
            NOTIFICATION_ID,
            buildNotification("Iniciando…"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
        )
        active = true
        running = true
        stopFlag = false

        if (data == null || resultCode != Activity.RESULT_OK) {
            fail("No diste permiso para capturar el audio.")
            return
        }

        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val proj = try {
            manager.getMediaProjection(resultCode, data)
        } catch (e: Exception) {
            fail("No pude iniciar la captura: ${e.message}")
            return
        }
        if (proj == null) {
            fail("El sistema no entregó la captura de audio.")
            return
        }
        val callback = object : MediaProjection.Callback() {
            override fun onStop() {
                stopEverything("El sistema detuvo la captura.")
                stopSelf()
            }
        }
        proj.registerCallback(callback, mainHandler) // obligatorio desde Android 14
        projection = proj
        projectionCallback = callback

        val rec = try {
            buildRecord(proj)
        } catch (e: Exception) {
            fail("No pude abrir la captura de audio: ${e.message}")
            return
        }
        record = rec

        acquireLocks()
        queue.clear()
        startCaptureThread(rec)
        startSenderThread()
        publish("Conectando a $host:$port…")
    }

    private fun buildRecord(proj: MediaProjection): AudioRecord {
        val config = AudioPlaybackCaptureConfiguration.Builder(proj)
            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(AudioAttributes.USAGE_GAME)
            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
            .build()
        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(SAMPLE_RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
            .build()
        val minBuffer = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT
        )
        val bufferSize = maxOf(minBuffer, CHUNK_FRAMES * FRAME_BYTES * 8)
        return AudioRecord.Builder()
            .setAudioFormat(format)
            .setBufferSizeInBytes(bufferSize)
            .setAudioPlaybackCaptureConfig(config)
            .build()
    }

    private fun startCaptureThread(rec: AudioRecord) {
        captureThread = Thread({
            val buffer = ByteArray(CHUNK_FRAMES * FRAME_BYTES)
            try {
                rec.startRecording()
                while (!stopFlag) {
                    val n = rec.read(buffer, 0, buffer.size)
                    if (n < 0) {
                        if (!stopFlag) {
                            mainHandler.post {
                                stopEverything("Error de captura de audio ($n).")
                                stopSelf()
                            }
                        }
                        break
                    }
                    val usable = n - n % FRAME_BYTES
                    if (usable > 0) {
                        val chunk = buffer.copyOf(usable)
                        if (!queue.offer(chunk)) {
                            queue.poll()        // descarta lo más viejo: preferimos poca latencia
                            queue.offer(chunk)
                        }
                    }
                }
            } catch (e: Exception) {
                if (!stopFlag) {
                    mainHandler.post {
                        stopEverything("Error de captura: ${e.message}")
                        stopSelf()
                    }
                }
            }
        }, "phonemix-capture").also { it.start() }
    }

    private fun startSenderThread() {
        senderThread = Thread({
            val silence = ByteArray(CHUNK_FRAMES * FRAME_BYTES)
            while (!stopFlag) {
                var socket: Socket? = null
                try {
                    publish("Conectando a $host:$port…")
                    socket = Socket().apply {
                        tcpNoDelay = true
                        keepAlive = true
                        connect(InetSocketAddress(this@StreamService.host, this@StreamService.port), 3000)
                    }
                    val out = socket.getOutputStream()
                    out.write(buildHeader())
                    out.flush()
                    queue.clear()
                    publish("Enviando audio a $host:$port")
                    while (!stopFlag) {
                        // Si no hay audio en cola, mandamos un poco de silencio para
                        // mantener viva la conexión.
                        val chunk = queue.poll(200, TimeUnit.MILLISECONDS) ?: silence
                        out.write(chunk)
                        out.flush()
                    }
                } catch (e: InterruptedException) {
                    break
                } catch (e: Exception) {
                    if (!stopFlag) publish("Sin conexión (${e.message ?: "error"}). Reintentando…")
                } finally {
                    try { socket?.close() } catch (_: Exception) {}
                }
                if (!stopFlag) {
                    try { Thread.sleep(1500) } catch (_: InterruptedException) { break }
                }
            }
        }, "phonemix-sender").also { it.start() }
    }

    private fun buildHeader(): ByteArray {
        var name = (Build.MODEL ?: "Android").toByteArray(Charsets.UTF_8)
        if (name.size > 60) name = name.copyOf(60)
        val bytes = ByteArrayOutputStream()
        val d = DataOutputStream(bytes)
        d.write("PMX1".toByteArray(Charsets.US_ASCII))
        d.writeInt(SAMPLE_RATE)   // big-endian
        d.writeByte(2)            // canales
        d.writeByte(name.size)
        d.write(name)
        d.flush()
        return bytes.toByteArray()
    }

    // ------------------------------------------------------------------ //

    private fun fail(message: String) {
        stopEverything(message)
        stopSelf()
    }

    @Synchronized
    private fun stopEverything(status: String) {
        if (!active) {
            publish(status)
            return
        }
        active = false
        stopFlag = true
        captureThread?.interrupt()
        senderThread?.interrupt()
        try { record?.stop() } catch (_: Exception) {}
        try { record?.release() } catch (_: Exception) {}
        record = null
        projectionCallback?.let { try { projection?.unregisterCallback(it) } catch (_: Exception) {} }
        try { projection?.stop() } catch (_: Exception) {}
        projection = null
        projectionCallback = null
        try { wifiLock?.takeIf { it.isHeld }?.release() } catch (_: Exception) {}
        try { cpuLock?.takeIf { it.isHeld }?.release() } catch (_: Exception) {}
        wifiLock = null
        cpuLock = null
        queue.clear()
        running = false
        publish(status)
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    private fun acquireLocks() {
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "phonemix:wifi").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {}
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            cpuLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "phonemix:cpu").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {}
    }

    private fun publish(text: String) {
        lastStatus = text
        sendBroadcast(Intent(ACTION_STATUS).setPackage(packageName).putExtra(EXTRA_STATUS, text))
        try {
            if (active) {
                getSystemService(NotificationManager::class.java)
                    .notify(NOTIFICATION_ID, buildNotification(text))
            }
        } catch (_: Exception) {}
    }

    private fun createChannel() {
        val channel = NotificationChannel(CHANNEL_ID, "Envío de audio", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val stopIntent = Intent(this, StreamService::class.java).setAction(ACTION_STOP)
        val stopPending = PendingIntent.getService(this, 0, stopIntent, PendingIntent.FLAG_IMMUTABLE)
        val openPending = PendingIntent.getActivity(
            this, 1, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val stopAction = Notification.Action.Builder(
            Icon.createWithResource(this, android.R.drawable.ic_media_pause), "Detener", stopPending
        ).build()
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle("PhoneMix")
            .setContentText(text)
            .setContentIntent(openPending)
            .setOngoing(true)
            .addAction(stopAction)
            .build()
    }
}
