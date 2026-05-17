package com.safedrop.android.photo

import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Bridge between the background tool registry (where `take_photo` runs
 * as a `suspend` handler) and the Compose UI thread that owns the
 * Activity Result launcher needed to invoke the system camera.
 *
 * Flow:
 *   1. Tool handler calls [capture]; this emits a [PhotoRequest] on
 *      [requests] and suspends on its [CompletableDeferred].
 *   2. The UI host (registered in MainActivity) is collecting from
 *      [requests]; it builds a temp-file URI, launches the camera, and
 *      on the ActivityResult callback calls [PhotoRequest.deliver] with
 *      the result.
 *   3. [capture] resumes and the handler returns to the dispatcher.
 *
 * A 120s default timeout protects against the case where the user
 * leaves the camera dialog open and never resolves it.
 */
sealed class PhotoResult {
    data class Success(val bytes: ByteArray, val mimeType: String) : PhotoResult()
    data class Cancelled(val reason: String) : PhotoResult()
}

class PhotoRequest {
    val id: String = UUID.randomUUID().toString()
    private val deferred = CompletableDeferred<PhotoResult>()

    fun deliver(result: PhotoResult) {
        if (!deferred.isCompleted) deferred.complete(result)
    }

    suspend fun await(timeoutMs: Long): PhotoResult =
        withTimeoutOrNull(timeoutMs) { deferred.await() }
            ?: PhotoResult.Cancelled("timeout waiting for camera")
}

class PhotoCapturer {
    private val _requests = MutableSharedFlow<PhotoRequest>(extraBufferCapacity = 4)
    val requests: SharedFlow<PhotoRequest> = _requests

    /** Suspend until the UI either delivers a photo or the user cancels. */
    suspend fun capture(timeoutMs: Long = 120_000): PhotoResult {
        val req = PhotoRequest()
        _requests.emit(req)
        return req.await(timeoutMs)
    }
}
