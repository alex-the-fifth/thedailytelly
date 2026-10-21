package com.dailytelly.app

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.*

private val playbackReports = CoroutineScope(SupervisorJob() + Dispatchers.IO)

internal class PlayerJavascriptBridge(
    private val ready: () -> Unit,
    private val time: (String) -> Unit,
    private val state: (String) -> Unit,
    private val error: (String) -> Unit,
) {
    @JavascriptInterface fun onReady() = ready()
    @JavascriptInterface fun onDuration(value: String) = Unit
    @JavascriptInterface fun onTime(value: String) = time(value)
    @JavascriptInterface fun onState(value: String) = state(value)
    @JavascriptInterface fun onError(value: String) = error(value)
}

/** Standard YouTube controls remain unobscured. No codec spoofing or player overlays. */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun PlayerScreen(video: Video, onExit: () -> Unit) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    var current by remember { mutableIntStateOf(video.position_seconds) }
    var ended by remember { mutableStateOf(false) }
    var playing by remember { mutableStateOf(false) }
    var failure by remember { mutableStateOf<String?>(null) }
    val handler = remember { Handler(Looper.getMainLooper()) }
    val lifecycle = LocalLifecycleOwner.current
    var webView by remember { mutableStateOf<WebView?>(null) }
    fun report(event: String) {
        val position = current
        playbackReports.launch { Api.progress(ctx, video.video_id, position, event) }
    }
    fun exit() { if (!ended) report("pause"); onExit() }
    BackHandler { exit() }
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP) {
                webView?.evaluateJavascript("if(typeof player !== 'undefined' && player)player.pauseVideo();", null)
                playing = false
                if (!ended) report("pause")
                webView?.onPause()
            } else if (event == Lifecycle.Event.ON_START) webView?.onResume()
        }
        lifecycle.lifecycle.addObserver(observer)
        onDispose { lifecycle.lifecycle.removeObserver(observer) }
    }
    LaunchedEffect(playing) { while (playing) { delay(10000); if (!ended) report("heartbeat") } }
    Column(Modifier.fillMaxSize().statusBarsPadding()) {
        TextButton(onClick = { exit() }) { Text("Back to today's videos") }
        if (failure != null) Text(failure!!, color = MaterialTheme.colorScheme.error)
        AndroidView(
            modifier = Modifier.fillMaxWidth().weight(1f),
            factory = { context -> WebView(context).apply {
                webView = this
                WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true
                settings.mediaPlaybackRequiresUserGesture = false
                addJavascriptInterface(PlayerJavascriptBridge(
                    ready = { handler.post { report("start") } },
                    time = { value -> handler.post { current = value.toFloatOrNull()?.toInt() ?: current } },
                    state = { value -> handler.post {
                        when (value.toIntOrNull()) {
                            1 -> { playing = true }
                            2 -> { playing = false; report("pause") }
                            0 -> { playing = false; ended = true; report("ended"); onExit() }
                        }
                    } },
                    error = { value -> handler.post {
                        if (value.toIntOrNull() in setOf(100,101,150)) {
                            playbackReports.launch { Api.unplayable(ctx,video.video_id) }
                        }
                        failure = "YouTube could not play this video. Try again later."
                    } },
                ), "AndroidBridge")
                webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                        if (request?.isForMainFrame != true) return false
                        val target = request.url
                        val own = android.net.Uri.parse(prefs.serverUrl)
                        if (target.scheme == own.scheme && target.host == own.host && target.port == own.port) return false
                        if (target.scheme in setOf("https","http")) {
                            runCatching { context.startActivity(Intent(Intent.ACTION_VIEW,target)) }
                        }
                        return true
                    }
                    override fun onReceivedHttpError(view: WebView?, request: WebResourceRequest?, response: android.webkit.WebResourceResponse?) {
                        if (request?.isForMainFrame == true) failure = "Video unavailable. Refresh your library or link this TV again."
                    }
                }
                val start = if (video.completed) 0 else video.position_seconds
                loadUrl(prefs.serverUrl + "/api/player?v=${video.video_id}&t=$start", mapOf("Authorization" to "Bearer ${prefs.token}"))
            } },
            onRelease = { it.destroy(); webView = null },
        )
    }
}
