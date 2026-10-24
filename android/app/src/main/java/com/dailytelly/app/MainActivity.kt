package com.dailytelly.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.clickable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RectangleShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import coil.compose.AsyncImage
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

sealed interface Screen {
    data object Today : Screen
    data class Player(val video: Video) : Screen
    data object Setup : Screen
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = lightColorScheme(
                primary = Color(0xFF183D34), onPrimary = Color.White,
                secondary = Color(0xFF63736C), secondaryContainer = Color(0xFFDCEBDC),
                background = Color(0xFFF6F5EE), onBackground = Color(0xFF183D34),
                surface = Color(0xFFF6F5EE), onSurface = Color(0xFF183D34),
            )) { Surface(Modifier.fillMaxSize()) { App() } }
        }
    }
}

private val DISPLAY_ZONE: TimeZone = TimeZone.getDefault()

@Composable
fun App() {
    val ctx = LocalContext.current
    var screen by remember {
        mutableStateOf<Screen>(if (Prefs(ctx).configured) Screen.Today else Screen.Setup)
    }
    // hoisted above the screen switch so list data, mode and scroll
    // positions all survive a trip to the player
    var today by remember { mutableStateOf<TodayResponse?>(null) }
    var history by remember { mutableStateOf<HistoryResponse?>(null) }
    var showHistory by remember { mutableStateOf(false) }
    var versionAlertShown by remember { mutableStateOf(false) }
    var introDone by remember { mutableStateOf(false) }
    var toastDone by remember { mutableStateOf(false) }
    val todayGrid = remember { LazyGridState() }
    val historyGrid = remember { LazyGridState() }

    when (val s = screen) {
        is Screen.Today -> Box(Modifier.fillMaxSize()) {
        TodayScreen(
            today = today, onToday = { today = it },
            history = history, onHistory = { history = it },
            showHistory = showHistory, onShowHistory = { showHistory = it },
            versionAlertShown = versionAlertShown,
            onVersionAlertShown = { versionAlertShown = true },
            todayGrid = todayGrid, historyGrid = historyGrid,
            onPlay = { screen = Screen.Player(it) },
            onSetup = { screen = Screen.Setup },
        )
        // brief "out of videos" note, shown once per launch after the intro
        val exhausted = today?.exhausted_channels.orEmpty()
        var toastVisible by remember { mutableStateOf(false) }
        LaunchedEffect(introDone, exhausted) {
            if (introDone && !toastDone && exhausted.isNotEmpty()) {
                toastDone = true
                toastVisible = true
                delay(4_500)
                toastVisible = false
            }
        }
        AnimatedVisibility(
            visible = toastVisible,
            enter = fadeIn() + slideInVertically { it / 2 },
            exit = fadeOut() + slideOutVertically { it / 2 },
            modifier = Modifier.align(Alignment.BottomCenter)
                .navigationBarsPadding().padding(bottom = 24.dp),
        ) {
            Surface(
                color = Color(0xFF2B2F3A),
                shape = RoundedCornerShape(12.dp),
                shadowElevation = 6.dp,
            ) {
                Text(
                    "No new videos left from " + exhausted.joinToString(", "),
                    Modifier.padding(horizontal = 18.dp, vertical = 12.dp),
                    color = Color.White, fontSize = 15.sp,
                )
            }
        }
        // launch intro: date, then each of today's channels one by one
        val t = today
        if (!introDone && t != null) {
            IntroOverlay(t, onDone = { introDone = true })
        }
        }
        is Screen.Player -> PlayerScreen(video = s.video, onExit = { screen = Screen.Today })
        is Screen.Setup -> SetupScreen(onDone = { screen = Screen.Today })
    }
}

private enum class Reveal { BOUNCE, TWIRL, POP }

@Composable
fun IntroOverlay(today: TodayResponse, onDone: () -> Unit) {
    val dayName = remember(today.date) {
        try {
            val d = SimpleDateFormat("yyyy-MM-dd", Locale.US).parse(today.date)
            SimpleDateFormat("EEEE", Locale.US).format(d!!)
        } catch (e: Exception) { "" }
    }
    var showHello by remember { mutableStateOf(false) }
    var lettersGo by remember { mutableStateOf(false) }
    var shownChannels by remember { mutableIntStateOf(0) }
    var leaving by remember { mutableStateOf(false) }
    var finished by remember { mutableStateOf(false) }
    val focus = remember { FocusRequester() }
    fun finish() { if (!leaving) leaving = true }
    // take focus so a TV remote press skips the intro instead of opening a video underneath
    LaunchedEffect(Unit) { runCatching { focus.requestFocus() } }

    // slow, kid-paced timeline
    LaunchedEffect(Unit) {
        delay(100); showHello = true
        delay(500); lettersGo = true
        delay(dayName.length * 110L + 1_000)          // let the day name settle
        for (i in today.channels.indices) {
            if (leaving) return@LaunchedEffect
            shownChannels = i + 1
            delay(1_300)
        }
        delay(2_500)
        finish()
    }
    LaunchedEffect(leaving) {
        if (leaving) { delay(600); finished = true; onDone() }
    }
    BackHandler(enabled = !finished) { finish() }

    AnimatedVisibility(
        visible = !leaving,
        enter = fadeIn(),
        exit = fadeOut(androidx.compose.animation.core.tween(600)),
    ) {
        Box(
            Modifier.fillMaxSize()
                .background(
                    Brush.radialGradient(
                        listOf(Color(0xFF6C836B), Color(0xFF315F4D), Color(0xFF183D34))
                    )
                )
                .focusRequester(focus)
                .onPreviewKeyEvent { finish(); true }
                .clickable(
                    interactionSource = remember { MutableInteractionSource() },
                    indication = null,
                ) { finish() },
            contentAlignment = Alignment.Center,
        ) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                val helloAlpha by androidx.compose.animation.core.animateFloatAsState(
                    if (showHello) 1f else 0f, androidx.compose.animation.core.tween(800),
                    label = "hello",
                )
                Text(
                    "Today is", color = Color.White, fontSize = 22.sp,
                    modifier = Modifier.graphicsLayer { alpha = helloAlpha },
                )
                Row {
                    dayName.forEachIndexed { i, ch -> HoppingLetter(ch, i, lettersGo) }
                }
                Spacer(Modifier.height(26.dp))
                if (today.channels.isEmpty() && lettersGo) {
                    Text("No channels today", color = Color.White, fontSize = 22.sp)
                }
                today.channels.forEachIndexed { i, ch ->
                    ChannelChip(ch, Reveal.entries[i % Reveal.entries.size], i < shownChannels)
                }
            }
            Text(
                "tap to skip",
                Modifier.align(Alignment.BottomCenter).navigationBarsPadding()
                    .padding(bottom = 22.dp),
                color = Color.White.copy(alpha = 0.7f), fontSize = 13.sp,
            )
        }
    }
}

/** One letter of the day name: hops up from below with a springy overshoot. */
@Composable
private fun HoppingLetter(ch: Char, index: Int, go: Boolean) {
    val y = remember { androidx.compose.animation.core.Animatable(70f) }
    val scale = remember { androidx.compose.animation.core.Animatable(0.3f) }
    val alpha = remember { androidx.compose.animation.core.Animatable(0f) }
    LaunchedEffect(go) {
        if (!go) return@LaunchedEffect
        delay(index * 110L)
        val bouncy = androidx.compose.animation.core.spring<Float>(
            dampingRatio = 0.35f, stiffness = androidx.compose.animation.core.Spring.StiffnessLow)
        launch { alpha.animateTo(1f, androidx.compose.animation.core.tween(250)) }
        launch { scale.animateTo(1f, bouncy) }
        y.animateTo(0f, bouncy)
    }
    Text(
        ch.toString(), color = Color.White, fontSize = 64.sp, fontWeight = FontWeight.Black,
        modifier = Modifier.graphicsLayer {
            translationY = y.value * density
            scaleX = scale.value; scaleY = scale.value
            this.alpha = alpha.value
        },
    )
}

/** Channel pill with a playful entrance: bounce-drop, twirl, or pop; avatar wiggles on landing. */
@Composable
private fun ChannelChip(ch: Channel, style: Reveal, visible: Boolean) {
    val alpha = remember { androidx.compose.animation.core.Animatable(0f) }
    val y = remember { androidx.compose.animation.core.Animatable(if (style == Reveal.BOUNCE) -700f else 0f) }
    val rot = remember { androidx.compose.animation.core.Animatable(if (style == Reveal.TWIRL) -540f else 0f) }
    val scale = remember { androidx.compose.animation.core.Animatable(
        when (style) { Reveal.BOUNCE -> 0.7f; Reveal.TWIRL -> 0f; Reveal.POP -> 0.15f }) }
    val wiggle = remember { androidx.compose.animation.core.Animatable(0f) }

    LaunchedEffect(visible) {
        if (!visible) return@LaunchedEffect
        val S = androidx.compose.animation.core.Spring
        launch { alpha.animateTo(1f, androidx.compose.animation.core.tween(300)) }
        when (style) {
            Reveal.BOUNCE -> {
                launch { scale.animateTo(1f, androidx.compose.animation.core.tween(500)) }
                y.animateTo(0f, androidx.compose.animation.core.spring(0.3f, S.StiffnessLow))
            }
            Reveal.TWIRL -> {
                launch { scale.animateTo(1f, androidx.compose.animation.core.spring(0.45f, S.StiffnessLow)) }
                rot.animateTo(0f, androidx.compose.animation.core.spring(0.5f, 60f))
            }
            Reveal.POP -> {
                scale.animateTo(1f, androidx.compose.animation.core.spring(0.25f, S.StiffnessMediumLow))
            }
        }
        // happy little wiggle of the avatar once landed
        for (angle in listOf(-14f, 12f, -8f, 5f, 0f)) {
            wiggle.animateTo(angle, androidx.compose.animation.core.tween(120))
        }
    }

    Row(
        Modifier.padding(vertical = 7.dp)
            .graphicsLayer {
                this.alpha = alpha.value
                translationY = y.value * density
                rotationZ = rot.value
                scaleX = scale.value; scaleY = scale.value
            }
            .background(Color.White.copy(alpha = 0.16f), RoundedCornerShape(50))
            .padding(start = 8.dp, end = 24.dp, top = 8.dp, bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        AsyncImage(
            model = ch.thumbnail_url,
            contentDescription = null,
            modifier = Modifier.size(56.dp)
                .graphicsLayer { rotationZ = wiggle.value }
                .clip(CircleShape)
                .background(Color.White.copy(alpha = 0.3f)),
            contentScale = ContentScale.Crop,
        )
        Spacer(Modifier.width(14.dp))
        Text(ch.title ?: "", color = Color.White, fontSize = 26.sp, fontWeight = FontWeight.ExtraBold)
    }
}

private fun isWatched(v: Video) = v.completed || v.watched_pct >= 95

/** "2026-07-10T11:33:45Z" -> Date, or null. */
private fun parseUtc(ts: String?): Date? {
    if (ts.isNullOrEmpty()) return null
    return try {
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }.parse(ts)
    } catch (e: Exception) { null }
}

/** Day header for history grouping, in device local time: Today / Yesterday / weekday + date. */
private fun dayLabel(d: Date, now: Date = Date()): String {
    val key = SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { timeZone = DISPLAY_ZONE }
    val dKey = key.format(d)
    if (dKey == key.format(now)) return "Today"
    if (dKey == key.format(Date(now.time - 86_400_000L))) return "Yesterday"
    return SimpleDateFormat("EEEE, MMM d", Locale.US).apply { timeZone = DISPLAY_ZONE }.format(d)
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun TodayScreen(
    today: TodayResponse?, onToday: (TodayResponse?) -> Unit,
    history: HistoryResponse?, onHistory: (HistoryResponse?) -> Unit,
    showHistory: Boolean, onShowHistory: (Boolean) -> Unit,
    versionAlertShown: Boolean, onVersionAlertShown: () -> Unit,
    todayGrid: LazyGridState, historyGrid: LazyGridState,
    onPlay: (Video) -> Unit,
    onSetup: () -> Unit,
) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var clockText by remember { mutableStateOf("") }
    val myVersion = remember { Prefs(ctx).appVersion }

    // wall clock (Eastern), refreshed every 5 s
    LaunchedEffect(Unit) {
        val fmt = SimpleDateFormat("h:mm a", Locale.US).apply { timeZone = DISPLAY_ZONE }
        while (true) {
            clockText = fmt.format(Date())
            delay(5_000)
        }
    }

    // keep the screen awake for a configurable window after each refresh
    // (returning from the player restarts it), then allow normal sleep
    val rootView = LocalView.current
    LaunchedEffect(today) {
        val minutes = Prefs(ctx).awakeMinutes
        if (minutes > 0) {
            rootView.keepScreenOn = true
            delay(minutes * 60_000L)
        }
        rootView.keepScreenOn = false
    }
    DisposableEffect(Unit) { onDispose { rootView.keepScreenOn = false } }

    fun refresh() {
        scope.launch {
            loading = true
            try {
                onToday(Api.today(ctx))
                if (showHistory) onHistory(Api.history(ctx))
                error = null
            } catch (e: Exception) {
                error = e.message ?: "Could not reach the server"
            }
            loading = false
        }
    }

    fun toggleHistory() {
        val next = !showHistory
        onShowHistory(next)
        if (next) scope.launch {
            try { onHistory(Api.history(ctx)) } catch (e: Exception) {
                error = e.message ?: "Could not load history"
            }
        }
    }

    // refresh every time the app comes to the foreground (incl. first open
    // and returning from the player — the observer replays ON_RESUME)
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val obs = LifecycleEventObserver { _, e ->
            if (e == Lifecycle.Event.ON_RESUME) refresh()
        }
        lifecycleOwner.lifecycle.addObserver(obs)
        onDispose { lifecycleOwner.lifecycle.removeObserver(obs) }
    }

    val outdated = today != null && myVersion > 0 && today.latest_app_version > myVersion

    // one-time alert per app launch; the banner below stays visible
    if (outdated && !versionAlertShown) {
        AlertDialog(
            onDismissRequest = onVersionAlertShown,
            confirmButton = { TextButton(onClick = onVersionAlertShown) { Text("OK") } },
            title = { Text("Update available") },
            text = {
                Text(
                    "A newer version of Daily Telly is installed on another device. " +
                        "Please ask a parent to install the latest version on this one."
                )
            },
        )
    }

    Scaffold(topBar = {
        Column(Modifier.statusBarsPadding()) {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(
                    Modifier.weight(1f).combinedClickable(
                        onClick = {}, onLongClick = onSetup, // long-press = parent setup
                    )
                ) {
                    Text(
                        if (showHistory) "History" else "Daily Telly",
                        fontSize = 22.sp, fontWeight = FontWeight.Bold,
                    )
                    today?.channels?.takeIf { it.isNotEmpty() }?.let { chs ->
                        Text(
                            "Today: " + chs.mapNotNull { it.title }.joinToString(", "),
                            fontSize = 13.sp, color = MaterialTheme.colorScheme.primary,
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
                today?.let {
                    Surface(
                        color = MaterialTheme.colorScheme.primaryContainer,
                        shape = RoundedCornerShape(50),
                    ) {
                        Text(
                            "Last hour: ${it.watched_last_hour}",
                            Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
                            fontSize = 12.sp,
                        )
                    }
                    Spacer(Modifier.width(6.dp))
                }
                TextButton(onClick = { toggleHistory() }) {
                    Text(if (showHistory) "Videos" else "History")
                }
                TextButton(onClick = { refresh() }) { Text("Refresh") }
                Text(
                    clockText,
                    Modifier.padding(start = 4.dp),
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Medium,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            if (outdated) {
                Text(
                    "This app is out of date — please install the latest version.",
                    Modifier.fillMaxWidth()
                        .background(MaterialTheme.colorScheme.errorContainer)
                        .padding(horizontal = 16.dp, vertical = 6.dp),
                    color = MaterialTheme.colorScheme.onErrorContainer,
                    fontSize = 13.sp,
                )
            }
        }
    }) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when {
                loading && today == null ->
                    CircularProgressIndicator(Modifier.align(Alignment.Center))
                error != null && today == null -> Column(
                    Modifier.align(Alignment.Center).padding(24.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Text(error!!, textAlign = TextAlign.Center)
                    Spacer(Modifier.height(12.dp))
                    Button(onClick = { refresh() }) { Text("Try again") }
                }
                showHistory -> HistoryGrid(history, historyGrid, onPlay)
                today != null && today.channels.isEmpty() -> Text(
                    "No channels are set up for today.",
                    Modifier.align(Alignment.Center),
                )
                today != null -> {
                    val videos = today.videos.filter { !isWatched(it) }
                    if (videos.isEmpty()) {
                        Text(
                            "All caught up — nothing new today!",
                            Modifier.align(Alignment.Center), fontSize = 17.sp,
                        )
                    } else {
                        LazyVerticalGrid(
                            state = todayGrid,
                            columns = GridCells.Adaptive(minSize = 168.dp),
                            contentPadding = PaddingValues(8.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            items(videos, key = { it.video_id }) { v ->
                                VideoCard(v, onClick = { onPlay(v) })
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun HistoryGrid(history: HistoryResponse?, state: LazyGridState, onPlay: (Video) -> Unit) {
    if (history == null) {
        Box(Modifier.fillMaxSize()) {
            CircularProgressIndicator(Modifier.align(Alignment.Center))
        }
        return
    }
    if (history.videos.isEmpty()) {
        Box(Modifier.fillMaxSize()) {
            Text("Nothing watched yet.", Modifier.align(Alignment.Center), fontSize = 17.sp)
        }
        return
    }
    // server already sorts by last watched (newest first); group by device local day
    val groups = remember(history) {
        history.videos.groupBy { v -> parseUtc(v.last_played_at)?.let { dayLabel(it) } ?: "Earlier" }
    }
    LazyVerticalGrid(
        state = state,
        columns = GridCells.Adaptive(minSize = 168.dp),
        contentPadding = PaddingValues(8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        groups.forEach { (day, vids) ->
            item(key = "h-$day", span = { GridItemSpan(maxLineSpan) }) {
                Text(
                    day,
                    Modifier.padding(start = 4.dp, top = 10.dp, bottom = 2.dp),
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                )
            }
            items(vids, key = { "v-${it.video_id}" }) { v ->
                VideoCard(v, onClick = { if (v.playable) onPlay(v) }, dimmed = !v.playable)
            }
        }
    }
}

@Composable
fun VideoCard(v: Video, onClick: () -> Unit, dimmed: Boolean = false) {
    // Keep a clear focus indicator for TV remotes while using a flat, square-edged card.
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    Card(
        onClick = onClick,
        interactionSource = interaction,
        shape = RectangleShape,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        modifier = Modifier
            .alpha(if (dimmed) 0.45f else 1f)
            .scale(if (focused) 1.04f else 1f)
            .border(
                width = if (focused) 3.dp else 0.dp,
                color = if (focused) MaterialTheme.colorScheme.primary else Color.Transparent,
                shape = RectangleShape,
            ),
    ) {
        Column {
            Box {
                AsyncImage(
                    model = v.thumbnail_url,
                    contentDescription = v.title,
                    modifier = Modifier.fillMaxWidth().aspectRatio(16f / 9f),
                    contentScale = ContentScale.Crop,
                )
                if (v.watched_pct > 0) {
                    LinearProgressIndicator(
                        progress = { v.watched_pct / 100f },
                        modifier = Modifier.fillMaxWidth().height(9.dp)
                            .align(Alignment.BottomCenter),
                        color = Color(0xFFFFC107),
                        trackColor = Color.Black.copy(alpha = 0.45f),
                    )
                }
                if (v.completed) {
                    Icon(
                        Icons.Filled.CheckCircle, "Watched",
                        Modifier.align(Alignment.TopEnd).padding(5.dp).height(22.dp),
                        tint = Color(0xFFFFC107),
                    )
                }
            }
            Column(Modifier.padding(horizontal = 10.dp, vertical = 9.dp)) {
                Text(
                    v.title,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 19.sp,
                )
                Text(
                    if (dimmed) "Not available today"
                    else "%d:%02d · %s".format(
                        v.duration_seconds / 60, v.duration_seconds % 60,
                        v.published_at.take(10),
                    ),
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.padding(top = 3.dp),
                )
            }
        }
    }
}

@Composable
fun SetupScreen(onDone: () -> Unit) {
    PairingScreen(onDone)
}
