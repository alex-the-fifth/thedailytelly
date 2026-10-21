package com.dailytelly.app

import android.content.Context
import java.io.IOException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

// ---------- models ----------

@Serializable
data class Channel(val title: String? = null, val thumbnail_url: String? = null)

@Serializable
data class Video(
    val video_id: String,
    val title: String,
    val thumbnail_url: String? = null,
    val duration_seconds: Int,
    val published_at: String = "",
    val position_seconds: Int = 0,
    val watched_pct: Int = 0,
    val view_count: Long = 0,
    val completed: Boolean = false,
    val last_played_at: String? = null,
    val playable: Boolean = true,
)

@Serializable
data class TodayResponse(
    val date: String = "",
    val weekday: Int = 0,
    val channels: List<Channel> = emptyList(),
    val videos: List<Video> = emptyList(),
    val watched_last_hour: Int = 0,
    val latest_app_version: Long = 0,
    val exhausted_channels: List<String> = emptyList(),
)

@Serializable
data class HistoryResponse(val videos: List<Video> = emptyList())

@Serializable
data class ProgressBody(val video_id: String, val position_seconds: Int, val event: String)

@Serializable
data class UnplayableBody(val video_id: String)

@Serializable
data class PairStart(val device_code: String, val user_code: String, val verification_uri: String, val expires_in: Int, val interval: Int)
@Serializable
data class PairResult(val status: String, val access_token: String? = null)
@Serializable
private data class PairPollBody(val device_code: String)

// ---------- prefs ----------

class Prefs(ctx: Context) {
    private val appCtx = ctx.applicationContext
    private val sp = ctx.getSharedPreferences("dailytelly", Context.MODE_PRIVATE)

    init {
        // Upgrades must not send the old shared token to a new multi-account service.
        if (sp.getInt("platform_auth_version", 0) < 1) {
            sp.edit().remove("token").putString("server_url", BuildConfig.PLATFORM_URL)
                .putInt("platform_auth_version", 1).apply()
        }
    }

    /** This build's explicit versionCode, set in app/build.gradle.kts. */
    val appVersion: Long
        get() = try {
            val info = appCtx.packageManager.getPackageInfo(appCtx.packageName, 0)
            if (android.os.Build.VERSION.SDK_INT >= 28) info.longVersionCode
            else @Suppress("DEPRECATION") info.versionCode.toLong()
        } catch (e: Exception) { 0L }
    var serverUrl: String
        get() = sp.getString("server_url", BuildConfig.PLATFORM_URL) ?: BuildConfig.PLATFORM_URL
        set(v) { sp.edit().putString("server_url", v.trim().trimEnd('/')).apply() }
    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) { sp.edit().putString("token", v.trim()).apply() }
    var awakeMinutes: Int
        get() = sp.getInt("awake_minutes", 5)
        set(v) { sp.edit().putInt("awake_minutes", v.coerceIn(0, 120)).apply() }
    val configured: Boolean get() = token.isNotEmpty()
}

// ---------- api client ----------

object Api {
    private val json = Json { ignoreUnknownKeys = true }
    private val client = OkHttpClient()
    private val jsonType = "application/json".toMediaType()
    private val pendingProgress = ArrayDeque<ProgressBody>() // offline queue

    suspend fun startPairing(): PairStart = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(BuildConfig.PLATFORM_URL + "/pair/start")
            .post("{}".toRequestBody(jsonType)).build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("Pairing is temporarily unavailable (HTTP ${response.code})")
            json.decodeFromString<PairStart>(response.body!!.string())
        }
    }
    suspend fun pollPairing(code: String): PairResult = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(BuildConfig.PLATFORM_URL + "/pair/poll")
            .post(json.encodeToString(PairPollBody(code)).toRequestBody(jsonType)).build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("Code expired or unavailable. Request a new code.")
            json.decodeFromString<PairResult>(response.body!!.string())
        }
    }

    private fun builder(p: Prefs, path: String): Request.Builder {
        val b = Request.Builder()
            .url(p.serverUrl + path)

        if (p.token.isNotEmpty()) b.header("Authorization", "Bearer ${p.token}")
        return b
    }

    suspend fun today(ctx: Context): TodayResponse = withContext(Dispatchers.IO) {
        val p = Prefs(ctx)
        flushPending(p)
        client.newCall(builder(p, "/api/today").build()).execute().use { r ->
            if (r.code == 401) throw IOException("This TV is no longer linked. Hold the title to link it again.")
            if (!r.isSuccessful) throw IOException("Server error (HTTP ${r.code})")
            json.decodeFromString<TodayResponse>(r.body!!.string())
        }
    }

    suspend fun history(ctx: Context): HistoryResponse = withContext(Dispatchers.IO) {
        val p = Prefs(ctx)
        client.newCall(builder(p, "/api/history").build()).execute().use { r ->
            if (!r.isSuccessful) throw IOException("Server error (HTTP ${r.code})")
            json.decodeFromString<HistoryResponse>(r.body!!.string())
        }
    }

    /** Fire-and-forget; failures are queued and retried before the next /today call. */
    suspend fun progress(ctx: Context, videoId: String, positionSeconds: Int, event: String) =
        withContext(Dispatchers.IO) {
            val body = ProgressBody(videoId, positionSeconds, event)
            try {
                send(Prefs(ctx), body)
            } catch (e: Exception) {
                synchronized(pendingProgress) {
                    pendingProgress.addLast(body)
                    while (pendingProgress.size > 200) pendingProgress.removeFirst()
                }
            }
        }

    private fun send(p: Prefs, body: ProgressBody) {
        val req = builder(p, "/api/progress")
            .post(json.encodeToString(body).toRequestBody(jsonType))
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful && r.code !in setOf(400, 403, 404, 422)) throw IOException("HTTP ${r.code}")
        }
    }

    /** Tell the server this video refuses embedded playback; it disappears from /today. */
    suspend fun unplayable(ctx: Context, videoId: String) = withContext(Dispatchers.IO) {
        try {
            val p = Prefs(ctx)
            val req = builder(p, "/api/unplayable")
                .post(json.encodeToString(UnplayableBody(videoId)).toRequestBody(jsonType))
                .build()
            client.newCall(req).execute().close()
        } catch (e: Exception) { /* best effort */ }
    }

    private fun flushPending(p: Prefs) {
        while (true) {
            val body = synchronized(pendingProgress) { pendingProgress.firstOrNull() } ?: return
            try {
                send(p, body)
                synchronized(pendingProgress) { pendingProgress.removeFirst() }
            } catch (e: Exception) {
                return // still offline; keep queue
            }
        }
    }
}
