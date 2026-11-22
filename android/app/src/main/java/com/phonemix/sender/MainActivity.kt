package com.phonemix.sender

import android.Manifest
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

class MainActivity : Activity() {

    companion object {
        private const val REQ_PERMISSIONS = 10
        private const val REQ_CAPTURE = 11
        private const val DISCOVERY_PORT = 48001
    }

    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var statusText: TextView
    private lateinit var startButton: Button
    private lateinit var findButton: Button

    private val prefs by lazy { getSharedPreferences("phonemix", MODE_PRIVATE) }

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            statusText.text = intent?.getStringExtra(StreamService.EXTRA_STATUS) ?: return
            refreshButton()
        }
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(28), dp(20), dp(28))
        }

        fun text(content: String, size: Float, bold: Boolean = false, topMargin: Int = 0): TextView {
            val marginPx = dp(topMargin)
            return TextView(this).apply {
                text = content
                textSize = size
                if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
                ).also { it.topMargin = marginPx }
            }
        }

        root.addView(text("PhoneMix", 28f, bold = true))
        root.addView(text("Envía el audio de este teléfono a tu PC para escucharlo en los mismos audífonos.", 14f, topMargin = 4))

        root.addView(text("Dirección de la PC", 13f, bold = true, topMargin = 24))
        hostEdit = EditText(this).apply {
            hint = "Ej.: 192.168.1.20"
            inputType = InputType.TYPE_CLASS_TEXT
            setSingleLine()
            setText(prefs.getString("host", ""))
        }
        root.addView(hostEdit)

        root.addView(text("Puerto", 13f, bold = true, topMargin = 8))
        portEdit = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
            setText(prefs.getInt("port", 48000).toString())
        }
        root.addView(portEdit)

        findButton = Button(this).apply {
            text = "Buscar PC en la red"
            setOnClickListener { discover() }
        }
        root.addView(findButton)

        startButton = Button(this).apply {
            textSize = 18f
            setOnClickListener {
                if (StreamService.running) stopStreaming() else startFlow()
            }
        }
        root.addView(startButton, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, dp(64)
        ).apply { topMargin = dp(12) })

        statusText = text(StreamService.lastStatus, 15f, bold = true, topMargin = 16).apply {
            gravity = Gravity.START
        }
        root.addView(statusText)

        root.addView(text(
            "Cómo conectar\n" +
                "• Wi-Fi: escribe la IP que muestra PhoneMix en la PC, o pulsa «Buscar PC».\n" +
                "• USB: en la PC pulsa «Conectar por USB» y aquí usa 127.0.0.1.\n" +
                "• Bluetooth: activa «Conexión Bluetooth» (anclaje) en este teléfono, une la PC a esa red y usa la IP que PhoneMix muestre para esa red.\n\n" +
                "Cuando Android pregunte qué compartir, elige «Toda la pantalla»: PhoneMix solo captura el audio, no la imagen.\n\n" +
                "Algunas apps (p. ej. Netflix) bloquean la captura de su audio.",
            13f, topMargin = 24
        ))

        setContentView(ScrollView(this).apply { addView(root) })
        refreshButton()
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(StreamService.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(statusReceiver, filter)
        }
        statusText.text = StreamService.lastStatus
        refreshButton()
    }

    override fun onStop() {
        unregisterReceiver(statusReceiver)
        super.onStop()
    }

    private fun refreshButton() {
        startButton.text = if (StreamService.running) "Detener" else "Iniciar envío"
    }

    // ------------------------------------------------------------------ //
    // Inicio / parada
    // ------------------------------------------------------------------ //

    private fun startFlow() {
        val host = hostEdit.text.toString().trim()
        if (host.isEmpty()) {
            Toast.makeText(this, "Escribe la dirección de la PC o pulsa «Buscar PC».", Toast.LENGTH_LONG).show()
            return
        }
        val port = portEdit.text.toString().trim().toIntOrNull() ?: 48000
        prefs.edit().putString("host", host).putInt("port", port).apply()

        val needed = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) needed.add(Manifest.permission.POST_NOTIFICATIONS)
        val missing = needed.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), REQ_PERMISSIONS)
        } else {
            requestProjection()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_PERMISSIONS) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                requestProjection()
            } else {
                statusText.text = "Necesito el permiso de audio para capturar el sonido del teléfono."
            }
        }
    }

    private fun requestProjection() {
        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        @Suppress("DEPRECATION")
        startActivityForResult(manager.createScreenCaptureIntent(), REQ_CAPTURE)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_CAPTURE) return
        if (resultCode != RESULT_OK || data == null) {
            statusText.text = "Cancelaste el permiso de captura."
            return
        }
        val intent = Intent(this, StreamService::class.java)
            .setAction(StreamService.ACTION_START)
            .putExtra(StreamService.EXTRA_RESULT_CODE, resultCode)
            .putExtra(StreamService.EXTRA_PROJECTION_DATA, data)
            .putExtra(StreamService.EXTRA_HOST, hostEdit.text.toString().trim())
            .putExtra(StreamService.EXTRA_PORT, portEdit.text.toString().trim().toIntOrNull() ?: 48000)
        startForegroundService(intent)
        statusText.text = "Iniciando…"
    }

    private fun stopStreaming() {
        startService(Intent(this, StreamService::class.java).setAction(StreamService.ACTION_STOP))
    }

    // ------------------------------------------------------------------ //
    // Búsqueda automática de la PC (UDP broadcast)
    // ------------------------------------------------------------------ //

    @Suppress("DEPRECATION")
    private fun directedBroadcast(): InetAddress? = try {
        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val info = wifi.dhcpInfo
        val b = (info.ipAddress and info.netmask) or info.netmask.inv()
        InetAddress.getByAddress(
            byteArrayOf(
                (b and 0xff).toByte(),
                ((b shr 8) and 0xff).toByte(),
                ((b shr 16) and 0xff).toByte(),
                ((b shr 24) and 0xff).toByte()
            )
        )
    } catch (_: Exception) {
        null
    }

    private fun discover() {
        findButton.isEnabled = false
        statusText.text = "Buscando la PC en la red…"
        Thread {
            var foundHost: String? = null
            var foundPort = 48000
            var foundName = ""
            try {
                DatagramSocket().use { socket ->
                    socket.broadcast = true
                    socket.soTimeout = 800
                    val message = "PMX?".toByteArray(Charsets.US_ASCII)
                    val targets = listOfNotNull(InetAddress.getByName("255.255.255.255"), directedBroadcast())
                    for (attempt in 1..3) {
                        for (target in targets) {
                            try {
                                socket.send(DatagramPacket(message, message.size, target, DISCOVERY_PORT))
                            } catch (_: Exception) {}
                        }
                        try {
                            val buffer = ByteArray(256)
                            val packet = DatagramPacket(buffer, buffer.size)
                            socket.receive(packet)
                            val reply = String(packet.data, 0, packet.length, Charsets.UTF_8)
                            if (reply.startsWith("PMX!")) {
                                val body = reply.removePrefix("PMX!")
                                val cut = body.lastIndexOf(':')
                                foundName = if (cut >= 0) body.substring(0, cut) else body
                                foundPort = if (cut >= 0) body.substring(cut + 1).toIntOrNull() ?: 48000 else 48000
                                foundHost = packet.address.hostAddress
                                break
                            }
                        } catch (_: SocketTimeoutException) {}
                    }
                }
            } catch (_: Exception) {}

            runOnUiThread {
                findButton.isEnabled = true
                val host = foundHost
                if (host != null) {
                    hostEdit.setText(host)
                    portEdit.setText(foundPort.toString())
                    statusText.text = "PC encontrada: $foundName ($host)"
                } else {
                    statusText.text = "No encontré la PC. Revisa que PhoneMix esté abierto y que ambos estén en la misma red, o escribe la IP a mano."
                }
            }
        }.start()
    }
}
