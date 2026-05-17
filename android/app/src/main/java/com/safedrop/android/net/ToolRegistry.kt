package com.safedrop.android.net

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject

/**
 * One callable that another SafeDrop peer can invoke remotely.
 *
 * The handler takes a JSONObject of arguments and returns either a
 * primitive (String/Number/Boolean), a JSONObject, a JSONArray, or null.
 * Throwing inside the handler turns into a structured `error` on the wire.
 */
data class ToolSpec(
    val name: String,
    val description: String,
    val inputSchema: JSONObject,
    val handler: (JSONObject) -> Any?,
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
    fun call(name: String, arguments: JSONObject): Any? {
        val spec = tools[name] ?: throw NoSuchElementException("unknown tool: $name")
        return spec.handler(arguments)
    }
}


// ---------- default tools shipped on every Android peer --------------------

/** Build the default registry with [system_info], [read_clipboard], [write_clipboard]. */
fun buildDefaultRegistry(context: Context): ToolRegistry {
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

    return reg
}
