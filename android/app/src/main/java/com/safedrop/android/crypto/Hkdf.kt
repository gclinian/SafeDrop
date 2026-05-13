package com.safedrop.android.crypto

import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * HKDF-SHA256 (RFC 5869). When [salt] is null the spec calls for HashLen
 * zero bytes — which matches Python's `cryptography.hazmat.primitives.kdf.hkdf.HKDF(salt=None)`.
 */
object Hkdf {
    fun derive(ikm: ByteArray, info: ByteArray, length: Int, salt: ByteArray? = null): ByteArray {
        require(length in 1..(255 * 32)) { "length out of range" }
        val effectiveSalt = salt ?: ByteArray(32)

        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(effectiveSalt, "HmacSHA256"))
        val prk = mac.doFinal(ikm)

        mac.init(SecretKeySpec(prk, "HmacSHA256"))
        val okm = ByteArray(length)
        var previous = ByteArray(0)
        var pos = 0
        var counter: Byte = 1
        while (pos < length) {
            mac.reset()
            mac.update(previous)
            mac.update(info)
            mac.update(counter)
            previous = mac.doFinal()
            val take = minOf(previous.size, length - pos)
            System.arraycopy(previous, 0, okm, pos, take)
            pos += take
            counter = (counter + 1).toByte()
        }
        return okm
    }
}
