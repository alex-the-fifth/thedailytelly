package com.dailytelly.app

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

@Composable
fun PairingScreen(onDone: () -> Unit) {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    var retry by remember { mutableIntStateOf(0) }
    var code by remember { mutableStateOf("") }
    var address by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var relink by remember { mutableStateOf(!prefs.configured) }
    LaunchedEffect(retry, relink) {
        if (!relink) return@LaunchedEffect
        code = ""; error = null
        try {
            val start = Api.startPairing()
            code = start.user_code
            address = start.verification_uri
            val deadline = android.os.SystemClock.elapsedRealtime() + start.expires_in * 1000L
            while (android.os.SystemClock.elapsedRealtime() < deadline) {
                delay(start.interval * 1000L)
                val result = Api.pollPairing(start.device_code)
                if (result.status == "linked") {
                    prefs.serverUrl = BuildConfig.PLATFORM_URL
                    prefs.token = result.access_token ?: kotlin.error("Missing device credential")
                    onDone()
                    return@LaunchedEffect
                }
            }
            error = "This code has expired. Request a fresh one."
        } catch (failure: Exception) {
            if (failure is kotlinx.coroutines.CancellationException) throw failure
            error = failure.message ?: "Could not connect. Please try again."
        }
    }
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("The Daily Telly", fontSize = 29.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(20.dp))
        if (!relink) {
            Text("This screen is linked to your household.")
            Spacer(Modifier.height(20.dp))
            Button(onClick = onDone) { Text("Back to today's videos") }
            TextButton(onClick = { prefs.token = ""; relink = true }) { Text("Link to another account") }
            Text("Unlink old devices from your account in a browser.", fontSize = 13.sp)
        } else {
            Text("On your phone or computer, visit:")
            Spacer(Modifier.height(10.dp))
            Text(address.ifEmpty { BuildConfig.PLATFORM_URL + "/activate" }, fontSize = 21.sp)
            Spacer(Modifier.height(22.dp))
            if (code.isEmpty() && error == null) CircularProgressIndicator()
            else if (error == null) Text(code, fontSize = 46.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(20.dp))
            Text("Sign in with your email, then enter this code. It expires in 10 minutes.")
            Text("Only confirm a code displayed on your own screen.", fontSize = 13.sp)
            if (error != null) {
                Spacer(Modifier.height(18.dp))
                Text(error!!, color = MaterialTheme.colorScheme.error)
                Button(onClick = { retry++ }) { Text("Get a new code") }
            }
        }
    }
}
