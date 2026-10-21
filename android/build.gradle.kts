plugins {
    id("com.android.application") version "9.2.1" apply false
    // AGP 9 has built-in Kotlin: org.jetbrains.kotlin.android is no longer applied.
    // Compiler plugins (Compose, serialization) are still separate; 2.2.10+ required.
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.10" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "2.2.10" apply false
}
