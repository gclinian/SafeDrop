package com.safedrop.android.ui

import java.util.Locale

fun humanSize(bytes: Long): String {
    if (bytes < 1024) return "${bytes} B"
    var v = bytes.toDouble()
    val units = arrayOf("KB", "MB", "GB", "TB")
    for (u in units) {
        v /= 1024.0
        if (v < 1024.0 || u == "TB") return String.format(Locale.US, "%.1f %s", v, u)
    }
    return "$bytes B"
}

fun humanSpeed(bytesPerSec: Double): String = "${humanSize(bytesPerSec.toLong())}/s"
