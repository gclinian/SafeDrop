package com.safedrop.android

import android.app.Application
import com.safedrop.android.data.SafeDropService

class SafeDropApplication : Application() {
    lateinit var service: SafeDropService
        private set

    override fun onCreate() {
        super.onCreate()
        service = SafeDropService(this).apply { start() }
    }

    override fun onTerminate() {
        service.stop()
        super.onTerminate()
    }
}
