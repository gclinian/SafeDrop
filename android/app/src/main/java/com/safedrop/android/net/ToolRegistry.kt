package com.safedrop.android.net

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.util.Base64
import androidx.core.app.NotificationCompat
import com.safedrop.android.photo.PhotoCapturer
import com.safedrop.android.photo.PhotoResult
import org.json.JSONArray
import org.json.JSONObject

/**
 * One callable that another SafeDrop peer can invoke remotely.
 *
 * The handler is a `suspend` function that takes a JSONObject of
 * arguments and returns either a primitive (String/Number/Boolean), a
 * JSONObject, a JSONArray, or null. Throwing inside the handler turns
 * into a structured `error` on the wire. Handlers that need to wait on
 * a user action (e.g. `take_photo`) can suspend without blocking the
 * inbound dispatcher thread.
 */
data class ToolSpec(
    val name: String,
    val description: String,
    val inputSchema: JSONObject,
    val handler: suspend (JSONObject) -> Any?,
) {
    fun manifest(): JSONObject = JSONObject().apply {
        put("name", name)
        put("description", description)
        put("inputSchema", inputSchema)
    }
}

/** Mutable registry of [ToolSpec]s served by this peer. */
class ToolRegistry {
    private val tools = linkedMapOf<String, ToolSpec>()

    fun register(spec: ToolSpec) {
        tools[spec.name] = spec
    }

    fun has(name: String): Boolean = name in tools

    fun listManifests(): JSONArray {
        val arr = JSONArray()
        for (t in tools.values) arr.put(t.manifest())
        return arr
    }

    /** @throws [NoSuchElementException] if no such tool. */
    suspend fun call(name: String, arguments: JSONObject): Any? {
        val spec = tools[name] ?: throw NoSuchElementException("unknown tool: $name")
        return spec.handler(arguments)
    }
}


// ---------- default tools shipped on every Android peer --------------------

/**
 * Build the default registry with [system_info], [read_clipboard],
 * [write_clipboard], and (if a [photoCapturer] is supplied) [take_photo].
 */
fun buildDefaultRegistry(
    context: Context,
    photoCapturer: PhotoCapturer? = null,
): ToolRegistry {
    val reg = ToolRegistry()
    val app = context.applicationContext

    reg.register(ToolSpec(
        name = "system_info",
        description = "Return basic info about this device: hostname, OS, machine, model.",
        inputSchema = JSONObject().apply {
            put("type", "object")
            put("properties", JSONObject())
        },
        handler = { _ ->
            JSONObject().apply {
                put("hostname", android.os.Build.MODEL)
                put("platform", "Android")
                put("release", android.os.Build.VERSION.RELEASE)
                put("sdk_int", android.os.Build.VERSION.SDK_INT)
                put("machine", android.os.Build.SUPPORTED_ABIS.firstOrNull() ?: "?")
                put("manufacturer", android.os.Build.MANUFACTURER)
                put("model", android.os.Build.MODEL)
            }
        },
    ))

    reg.register(ToolSpec(
        name = "read_clipboard",
        description =
            "Read the current clipboard contents on this device. Requires the SafeDrop app " +
            "to be foreground on Android 10+ — backgrounded apps can't access the clipboard.",
        inputSchema = JSONObject().apply {
            put("type", "object")
            put("properties", JSONObject())
        },
        handler = { _ ->
            val cm = app.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            val text = cm?.primaryClip?.takeIf { it.itemCount > 0 }
                ?.getItemAt(0)
                ?.coerceToText(app)
                ?.toString() ?: ""
            JSONObject().apply {
                put("content", text)
                put("content_type", "text")
            }
        },
    ))

    reg.register(ToolSpec(
        name = "write_clipboard",
        description = "Set this device's clipboard to the given text.",
        inputSchema = JSONObject().apply {
            put("type", "object")
            put("properties", JSONObject().apply {
                put("content", JSONObject().apply {
                    put("type", "string")
                    put("description", "Text to write to the clipboard.")
                })
            })
            put("required", JSONArray().put("content"))
        },
        handler = { args ->
            val content = args.optString("content", "")
            val cm = app.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            cm?.setPrimaryClip(ClipData.newPlainText("SafeDrop", content))
            JSONObject().apply {
                put("status", "ok")
                put("wrote_chars", content.length)
            }
        },
    ))

    // ---- show_notification (v1.6) ------------------------------------
    // Drops a system-tray notification via NotificationManager. The
    // POST_NOTIFICATIONS permission is requested on first launch for
    // Android 13+; channels are required from API 26+. We create the
    // channel lazily on first call so the registry build is cheap.
    val notificationChannelId = "safedrop.peer-notifications"
    var channelEnsured = false
    reg.register(ToolSpec(
        name = "show_notification",
        description =
            "Show a system notification on this Android device via " +
            "NotificationManager. Renders in the notification shade. " +
            "Returns once enqueued — no user interaction expected.",
        inputSchema = JSONObject().apply {
            put("type", "object")
            put("properties", JSONObject().apply {
                put("title", JSONObject().put("type", "string"))
                put("body",  JSONObject().put("type", "string"))
                put("level", JSONObject().apply {
                    put("type", "string")
                    put("default", "info")
                })
            })
        },
        handler = { args ->
            val title = args.optString("title", "").ifEmpty { "SafeDrop" }
            val body  = args.optString("body", "")
            if (title.isEmpty() && body.isEmpty()) {
                throw IllegalArgumentException("title or body required")
            }
            val nm = app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

            // Lazy-create channel on API 26+.
            if (!channelEnsured && Build.VERSION.SDK_INT >= 26) {
                val ch = NotificationChannel(
                    notificationChannelId,
                    "SafeDrop peer notifications",
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply {
                    description = "Banners pushed via show_notification from paired SafeDrop peers."
                }
                nm.createNotificationChannel(ch)
                channelEnsured = true
            }

            val notif = NotificationCompat.Builder(app, notificationChannelId)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .build()
            nm.notify(System.currentTimeMillis().toInt(), notif)
            JSONObject().apply { put("status", "shown") }
        },
    ))

    if (photoCapturer != null) {
        reg.register(ToolSpec(
            name = "take_photo",
            description =
                "Capture a photo with this device's camera. The user has to be holding the " +
                "phone unlocked and the SafeDrop app foreground — the system camera UI opens, " +
                "the user takes a shot, and the resulting JPEG comes back to the caller. " +
                "Returns {mime_type, size_bytes, data_b64}.",
            inputSchema = JSONObject().apply {
                put("type", "object")
                put("properties", JSONObject().apply {
                    put("timeout_seconds", JSONObject().apply {
                        put("type", "integer")
                        put("default", 120)
                        put("description", "Max seconds to wait for the user to take the shot.")
                    })
                })
            },
            handler = { args ->
                val timeoutSeconds = args.optInt("timeout_seconds", 120)
                val outcome = photoCapturer.capture(timeoutSeconds * 1000L)
                when (outcome) {
                    is PhotoResult.Success -> JSONObject().apply {
                        put("mime_type", outcome.mimeType)
                        put("size_bytes", outcome.bytes.size)
                        put("data_b64", Base64.encodeToString(outcome.bytes, Base64.NO_WRAP))
                    }
                    is PhotoResult.Cancelled -> throw RuntimeException(
                        "photo capture failed: ${outcome.reason}"
                    )
                }
            },
        ))
    }

    return reg
}
