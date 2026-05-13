package com.safedrop.android.net

import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import org.json.JSONObject

private const val MAX_FRAME = 64 * 1024 * 1024  // 64 MB cap, matches Python.

object Protocol {
    fun sendFrame(out: OutputStream, payload: ByteArray) {
        require(payload.size <= MAX_FRAME) { "frame too large (${payload.size} bytes)" }
        val header = ByteArray(4)
        val len = payload.size
        header[0] = (len ushr 24).toByte()
        header[1] = (len ushr 16).toByte()
        header[2] = (len ushr 8).toByte()
        header[3] = (len and 0xFF).toByte()
        out.write(header)
        out.write(payload)
        out.flush()
    }

    fun recvFrame(input: InputStream): ByteArray {
        val header = ByteArray(4)
        readExact(input, header)
        val len =
            ((header[0].toInt() and 0xFF) shl 24) or
            ((header[1].toInt() and 0xFF) shl 16) or
            ((header[2].toInt() and 0xFF) shl 8) or
             (header[3].toInt() and 0xFF)
        require(len in 0..MAX_FRAME) { "frame too large ($len bytes)" }
        val payload = ByteArray(len)
        readExact(input, payload)
        return payload
    }

    fun sendJson(out: OutputStream, msg: JSONObject, encrypt: ((ByteArray) -> ByteArray)? = null) {
        val raw = msg.toString().toByteArray(Charsets.UTF_8)
        val payload = encrypt?.invoke(raw) ?: raw
        sendFrame(out, payload)
    }

    fun recvJson(input: InputStream, decrypt: ((ByteArray) -> ByteArray)? = null): JSONObject {
        val raw = recvFrame(input)
        val decoded = decrypt?.invoke(raw) ?: raw
        return JSONObject(decoded.toString(Charsets.UTF_8))
    }

    private fun readExact(input: InputStream, into: ByteArray) {
        var read = 0
        while (read < into.size) {
            val n = input.read(into, read, into.size - read)
            if (n < 0) throw EOFException("peer closed connection mid-frame")
            read += n
        }
    }
}
