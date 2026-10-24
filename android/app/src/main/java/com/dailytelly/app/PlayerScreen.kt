package com.dailytelly.app

import android.annotation.SuppressLint
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ClosedCaption
import androidx.compose.material.icons.filled.ClosedCaptionOff
import androidx.compose.material.icons.filled.Forward10
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Replay10
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Injected into YouTube's player iframe at document start (h264ify technique):
 * reports VP8/VP9/AV1 and >30fps as unsupported, so YouTube serves H.264 <=30fps,
 * which every Android device decodes in hardware. This also avoids HDR streams
 * (YouTube HDR is VP9.2/AV1 only), whose tone-mapping stutters in bright scenes.
 */
private const val COMPAT_PLAYBACK_JS = """
(function () {
  var BLOCK = /vp0?8|vp0?9|av01/i;
  function tooFast(t) { var m = /framerate=(\d+)/i.exec(t || ""); return !!m && parseInt(m[1], 10) > 30; }
  function bad(t) { return BLOCK.test(t || "") || tooFast(t); }
  try {
    if (window.MediaSource && MediaSource.isTypeSupported) {
      var origMse = MediaSource.isTypeSupported.bind(MediaSource);
      MediaSource.isTypeSupported = function (t) { return bad(t) ? false : origMse(t); };
    }
    var proto = window.HTMLMediaElement && HTMLMediaElement.prototype;
    if (proto && proto.canPlayType) {
      var origCan = proto.canPlayType;
      proto.canPlayType = function (t) { return bad(t) ? "" : origCan.call(this, t); };
    }
    var mc = navigator.mediaCapabilities;
    if (mc && mc.decodingInfo) {
      var origInfo = mc.decodingInfo.bind(mc);
      mc.decodingInfo = function (cfg) {
        var v = cfg && cfg.video;
        if (v && (bad(v.contentType) || (v.framerate && v.framerate > 30))) {
          return Promise.resolve({ supported: false, smooth: false, powerEfficient: false });
        }
        return origInfo(cfg);
      };
    }
  } catch (e) {}
})();
"""

// survives screen disposal so exit-events still get sent
private val reportScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

private class PlayerJavascriptBridge(
    private val ready: () -> Unit,
    private val duration: (String) -> Unit,
    private val time: (String) -> Unit,
    private val state: (String) -> Unit,
    private val error: (String) -> Unit,
) {
    @JavascriptInterface fun onReady() = ready()
    @JavascriptInterface fun onDuration(value: String) = duration(value)
    @JavascriptInterface fun onTime(value: String) = time(value)
    @JavascriptInterface fun onState(value: String) = state(value)
    @JavascriptInterface fun onError(value: String) = error(value)
}

@SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
@Composable
fun PlayerScreen(video: Video, onExit: () -> Unit) {
    // allows inspecting the player WebView from desktop Chrome via chrome://inspect
    LaunchedEffect(Unit) { WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG) }

    val ctx = LocalContext.current
    var webView by remember { mutableStateOf<WebView?>(null) }
    var playing by remember { mutableStateOf(false) }
    var currentSec by remember { mutableFloatStateOf(video.position_seconds.toFloat()) }
    var durationSec by remember { mutableFloatStateOf(video.duration_seconds.toFloat()) }
    var seeking by remember { mutableStateOf(false) }
    var sliderPos by remember { mutableFloatStateOf(video.position_seconds.toFloat()) }
    var ended by remember { mutableStateOf(false) }
    var ccOn by remember { mutableStateOf(false) }
    var startReported by remember { mutableStateOf(false) }
    var controlsVisible by remember { mutableStateOf(true) }
    var clockText by remember { mutableStateOf("") }
    val playPauseFocus = remember { FocusRequester() }

    // wall clock, device local time, refreshed every 5 s
    LaunchedEffect(Unit) {
        val fmt = SimpleDateFormat("h:mm a", Locale.US).apply {
            timeZone = TimeZone.getDefault()
        }
        while (true) {
            clockText = fmt.format(Date())
            delay(5_000)
        }
    }

    val rootView = LocalView.current
    DisposableEffect(Unit) {
        rootView.keepScreenOn = true
        onDispose { rootView.keepScreenOn = false }  // let the list screen sleep
    }

    fun report(event: String) {
        val pos = currentSec.toInt()
        reportScope.launch { Api.progress(ctx, video.video_id, pos, event) }
    }

    fun js(cmd: String) = webView?.evaluateJavascript(cmd, null)

    fun playerUrl(start: Int, cc: Boolean) = Prefs(ctx).serverUrl +
        "/api/player?v=${video.video_id}&t=$start&cc=${if (cc) 1 else 0}"

    fun seekBy(delta: Float) {
        val target = (currentSec + delta).coerceIn(0f, maxOf(durationSec - 1f, 0f))
        js("appSeek($target)")
        currentSec = target
        sliderPos = target
    }

    fun togglePlay() { if (playing) js("appPause()") else js("appPlay()") }

    fun exit(finalEvent: String) {
        if (!ended) report(finalEvent)
        onExit()
    }

    BackHandler { exit("pause") }

    // heartbeat every 10 s while playing
    LaunchedEffect(playing) {
        while (playing) {
            delay(10_000)
            report("heartbeat")
        }
    }

    // auto-hide controls after 4 s of playback; keep visible while paused/seeking
    LaunchedEffect(playing, controlsVisible, seeking) {
        if (playing && controlsVisible && !seeking) {
            delay(4_000)
            controlsVisible = false
        }
    }
    LaunchedEffect(controlsVisible) {
        if (controlsVisible) {
            delay(100)
            runCatching { playPauseFocus.requestFocus() }
        }
    }

    val mainHandler = remember { Handler(Looper.getMainLooper()) }
    val bridge = remember {
        PlayerJavascriptBridge(
            ready = {
                mainHandler.post {
                    if (!startReported) { startReported = true; report("start") }
                }
            },
            duration = { value ->
                mainHandler.post {
                    value.toFloatOrNull()?.let { if (it > 0f) durationSec = it }
                }
            },
            time = { value ->
                mainHandler.post {
                    val seconds = value.toFloatOrNull() ?: return@post
                    currentSec = seconds
                    if (!seeking) sliderPos = seconds
                }
            },
            state = { value ->
                mainHandler.post {
                    when (value.toIntOrNull()) {
                        1 -> playing = true
                        2 -> {
                            playing = false
                            controlsVisible = true
                            report("pause")
                        }
                        0 -> {
                            playing = false
                            ended = true
                            currentSec = durationSec
                            report("ended")
                            onExit()
                        }
                    }
                }
            },
            error = { value ->
                mainHandler.post {
                    ended = true
                    if (value.toIntOrNull() in setOf(100, 101, 150)) {
                        reportScope.launch { Api.unplayable(ctx, video.video_id) }
                    }
                    onExit()
                }
            },
        )
    }

    val navKeys = remember {
        setOf(
            KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN,
            KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT,
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER,
        )
    }

    Box(
        Modifier.fillMaxSize().background(Color.Black)
            .onPreviewKeyEvent { e ->
                if (e.type != KeyEventType.KeyUp) return@onPreviewKeyEvent false
                val code = e.nativeKeyEvent.keyCode
                when (code) {
                    KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> { togglePlay(); true }
                    KeyEvent.KEYCODE_MEDIA_PLAY -> { js("appPlay()"); true }
                    KeyEvent.KEYCODE_MEDIA_PAUSE -> { js("appPause()"); true }
                    KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> { seekBy(10f); true }
                    KeyEvent.KEYCODE_MEDIA_REWIND -> { seekBy(-10f); true }
                    // any nav key while controls are hidden: just reveal them
                    in navKeys -> if (!controlsVisible) { controlsVisible = true; true } else false
                    else -> false
                }
            },
    ) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { c ->
                WebView(c).apply {
                    layoutParams = ViewGroup.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT,
                    )
                    settings.javaScriptEnabled = true
                    settings.mediaPlaybackRequiresUserGesture = false
                    // storage must stay ENABLED (YouTube's player crashes without it),
                    // but we wipe it each time so the caption preference can't persist
                    settings.domStorageEnabled = true
                    android.webkit.WebStorage.getInstance().deleteAllData()
                    android.webkit.CookieManager.getInstance().removeAllCookies(null)
                    // never take input focus -> no soft-keyboard popup
                    isFocusable = false
                    isFocusableInTouchMode = false
                    webChromeClient = object : WebChromeClient() {
                        override fun onConsoleMessage(
                            msg: android.webkit.ConsoleMessage,
                        ): Boolean {
                            android.util.Log.d("DailyTellyPlayer",
                                "[js] ${msg.message()} (${msg.sourceId()}:${msg.lineNumber()})")
                            return true
                        }
                    }
                    webViewClient = object : WebViewClient() {
                        override fun onReceivedHttpError(
                            view: WebView?, request: WebResourceRequest?,
                            errorResponse: android.webkit.WebResourceResponse?,
                        ) {
                            if (request?.isForMainFrame == true) {
                                android.widget.Toast.makeText(
                                    c, "Video unavailable. Check your connection, token, and today's rules.",
                                    android.widget.Toast.LENGTH_LONG,
                                ).show()
                                onExit()
                            }
                        }

                        override fun onReceivedError(
                            view: WebView?, request: WebResourceRequest?,
                            error: android.webkit.WebResourceError?,
                        ) {
                            if (request?.isForMainFrame == true) {
                                android.widget.Toast.makeText(
                                    c, "Could not load the player. Check your connection.",
                                    android.widget.Toast.LENGTH_LONG,
                                ).show()
                                onExit()
                            }
                        }

                        // Keep navigation on the configured server origin.
                        override fun shouldOverrideUrlLoading(
                            view: WebView?, request: WebResourceRequest?,
                        ): Boolean {
                            val target = request?.url ?: return true
                            val ownHost = android.net.Uri.parse(Prefs(c).serverUrl).host
                            val blocked = target.host == null || target.host != ownHost ||
                                target.scheme != android.net.Uri.parse(Prefs(c).serverUrl).scheme ||
                                target.port != android.net.Uri.parse(Prefs(c).serverUrl).port
                            if (blocked) {
                                android.util.Log.d("DailyTellyPlayer",
                                    "blocked navigation to $target")
                            }
                            return blocked
                        }
                    }
                    addJavascriptInterface(bridge, "AndroidBridge")
                    // hardware layer for smoother video compositing
                    setLayerType(android.view.View.LAYER_TYPE_HARDWARE, null)
                    val start = if (video.completed || video.position_seconds >
                        video.duration_seconds * 0.95) 0 else video.position_seconds
                    val prefs = Prefs(c)
                    loadUrl(playerUrl(start, ccOn), mapOf(
                        "Authorization" to "Bearer ${prefs.token}",
                    ))
                    webView = this
                }
            },
            onRelease = { it.destroy() },
        )

        // invisible tap-catcher over the video: toggles the controls,
        // and stops the child poking YouTube's own UI
        Box(
            Modifier.fillMaxSize().clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null,
            ) { controlsVisible = !controlsVisible }
        )

        // always-visible playback progress: yellow bar growing along the bottom
        if (durationSec > 0f) {
            Box(
                Modifier.align(Alignment.BottomStart)
                    .fillMaxWidth((currentSec / durationSec).coerceIn(0f, 1f))
                    .height(9.dp)
                    .background(Color(0xFFFFEB3B))
            )
        }

        // persistent wall-clock overlay, top-left
        Text(
            clockText,
            Modifier.align(Alignment.TopStart).statusBarsPadding().padding(10.dp)
                .background(Color.Black.copy(alpha = 0.5f), MaterialTheme.shapes.small)
                .padding(horizontal = 8.dp, vertical = 3.dp),
            color = Color.White,
            fontSize = 13.sp,
        )

        AnimatedVisibility(
            visible = controlsVisible,
            enter = fadeIn(), exit = fadeOut(),
            modifier = Modifier.align(Alignment.BottomCenter),
        ) {
            Column(
                Modifier.fillMaxWidth()
                    .background(
                        Brush.verticalGradient(
                            listOf(Color.Transparent, Color.Black.copy(alpha = 0.75f))
                        )
                    )
                    .navigationBarsPadding()
                    .padding(horizontal = 12.dp)
                    .padding(top = 24.dp, bottom = 4.dp),
            ) {
                Slider(
                    value = sliderPos,
                    onValueChange = { sliderPos = it; seeking = true },
                    onValueChangeFinished = {
                        js("appSeek($sliderPos)")
                        currentSec = sliderPos
                        seeking = false
                    },
                    valueRange = 0f..maxOf(durationSec, 1f),
                    modifier = Modifier.fillMaxWidth().height(24.dp),
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = { exit("pause") }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back", tint = Color.White)
                    }
                    IconButton(onClick = { seekBy(-10f) }) {
                        Icon(Icons.Filled.Replay10, "Back 10 seconds", tint = Color.White)
                    }
                    IconButton(
                        onClick = { togglePlay() },
                        modifier = Modifier.focusRequester(playPauseFocus),
                    ) {
                        Icon(
                            if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow,
                            if (playing) "Pause" else "Play",
                            tint = Color.White,
                        )
                    }
                    IconButton(onClick = { seekBy(10f) }) {
                        Icon(Icons.Filled.Forward10, "Forward 10 seconds", tint = Color.White)
                    }
                    IconButton(onClick = {
                        ccOn = !ccOn
                        playing = false
                        webView?.loadUrl(playerUrl(currentSec.toInt(), ccOn), mapOf(
                            "Authorization" to "Bearer ${Prefs(ctx).token}",
                        ))
                    }) {
                        Icon(
                            if (ccOn) Icons.Filled.ClosedCaption
                            else Icons.Filled.ClosedCaptionOff,
                            "Subtitles",
                            tint = if (ccOn) MaterialTheme.colorScheme.primary else Color.White,
                        )
                    }
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "%d:%02d / %d:%02d".format(
                            currentSec.toInt() / 60, currentSec.toInt() % 60,
                            durationSec.toInt() / 60, durationSec.toInt() % 60,
                        ),
                        color = Color.White,
                    )
                }
            }
        }
    }
}
