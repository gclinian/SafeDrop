package com.safedrop.android.crypto

import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Minimal Fernet implementation — interoperable with Python's
 * `cryptography.fernet.Fernet`.
 *
 * Token layout (before base64-url encoding):
 *     0x80 | timestamp(8 BE) | iv(16) | ciphertext | hmac-sha256(32)
 *
 * The 32-byte key is split:
 *     bytes[0..16) = HMAC signing key
 *     bytes[16..32) = AES-128 encryption key
 */
class Fernet(keyBytes: ByteArray) {
    init { require(keyBytes.size == 32) { "Fernet key must be 32 bytes" } }

    private val signingKey: ByteArray = keyBytes.copyOfRange(0, 16)
    private val encKey: ByteArray = keyBytes.copyOfRange(16, 32)
    private val rng = SecureRandom()

    fun encrypt(plaintext: ByteArray, timestamp: Long = System.currentTimeMillis() / 1000): ByteArray {
        val iv = ByteArray(16).also { rng.nextBytes(it) }
        val cipher = Cipher.getInstance("AES/CBC/PKCS5Padding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(encKey, "AES"), IvParameterSpec(iv))
        val ct = cipher.doFinal(plaintext)

        val header = ByteBuffer.allocate(1 + 8 + 16).apply {
            put(0x80.toByte())
            putLong(timestamp)
            put(iv)
        }.array()

        val body = ByteArray(header.size + ct.size).also {
            System.arraycopy(header, 0, it, 0, header.size)
            System.arraycopy(ct, 0, it, header.size, ct.size)
        }

        val hmac = Mac.getInstance("HmacSHA256").apply {
            init(SecretKeySpec(signingKey, "HmacSHA256"))
        }.doFinal(body)

        val token = ByteArray(body.size + hmac.size)
        System.arraycopy(body, 0, token, 0, body.size)
        System.arraycopy(hmac, 0, token, body.size, hmac.size)
        return Base64.getUrlEncoder().encode(token)
    }

    fun decrypt(token: ByteArray): ByteArray {
        val data = try {
            Base64.getUrlDecoder().decode(token)
        } catch (e: IllegalArgumentException) {
            throw IllegalArgumentException("Invalid base64 token", e)
        }
        require(data.size >= 1 + 8 + 16 + 32 + 16) { "token too short" }
        require(data[0] == 0x80.toByte()) { "bad version byte ${data[0]}" }

        val hmacOffset = data.size - 32
        val body = data.copyOfRange(0, hmacOffset)
        val tag = data.copyOfRange(hmacOffset, data.size)

        val expected = Mac.getInstance("HmacSHA256").apply {
            init(SecretKeySpec(signingKey, "HmacSHA256"))
        }.doFinal(body)

        if (!MessageDigest.isEqual(expected, tag)) {
            throw SecurityException("HMAC mismatch — bad key or tampered token")
        }

        val iv = data.copyOfRange(9, 25)
        val ct = data.copyOfRange(25, hmacOffset)

        val cipher = Cipher.getInstance("AES/CBC/PKCS5Padding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(encKey, "AES"), IvParameterSpec(iv))
        return cipher.doFinal(ct)
    }
}
