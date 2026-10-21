plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "com.dailytelly.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.dailytelly.app"
        minSdk = 24
        targetSdk = 35
        val platformUrl = providers.gradleProperty("platformUrl").orElse("https://thedailytelly.com").get()
        require(platformUrl.startsWith("https://") || platformUrl.startsWith("http://127.0.0.1:") || platformUrl.startsWith("http://10.0.2.2:"))
        buildConfigField("String", "PLATFORM_URL", "\"$platformUrl\"")
        versionCode = (System.currentTimeMillis() / 60_000L).toInt()
        versionName = "2.0.$versionCode"
    }
    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    buildFeatures { compose = true; buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        // Kotlin jvmTarget follows targetCompatibility automatically with
        // AGP 9 built-in Kotlin (the old kotlinOptions block is removed).
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.09.03"))
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")
    implementation("io.coil-kt:coil-compose:2.6.0")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
}
