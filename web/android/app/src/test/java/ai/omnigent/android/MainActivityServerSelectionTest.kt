package ai.omnigent.android

import android.content.Context
import android.os.Looper
import android.view.View
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import java.time.Duration
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * The floating server-switcher pill's web-driven lifecycle. The pill used to
 * be unconditionally visible, floating across the chat header's Chat/Terminal
 * toggle on phone-width screens; now the web steers it over the bridge
 * (selection lives in the SPA's sidebar picker) and a liveness watchdog
 * reveals it only on pages that never speak the protocol, keeping it as the
 * recovery path for older web builds and broken loads.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MainActivityServerSelectionTest {
    private fun launch(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    private fun idlePastWatchdog() {
        // Just past MainActivity.SWITCHER_LIVENESS_TIMEOUT_MS (6s).
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(7))
    }

    @Test
    fun `pill starts hidden and the watchdog reveals it when the page never speaks`() {
        val activity = launch()
        val pill = activity.switchButton()

        assertEquals(View.GONE, pill.visibility)

        // No bridge traffic (an older web build, a login page): recovery path.
        idlePastWatchdog()
        assertEquals(View.VISIBLE, pill.visibility)
    }

    @Test
    fun `a web hide request keeps the pill down past the watchdog`() {
        val activity = launch()
        val pill = activity.switchButton()

        activity.serverSwitcherHidden(true)
        idlePastWatchdog()

        assertEquals(View.GONE, pill.visibility)
    }

    @Test
    fun `the web can show and re-hide the pill`() {
        val activity = launch()
        val pill = activity.switchButton()

        activity.serverSwitcherHidden(false)
        assertEquals(View.VISIBLE, pill.visibility)

        activity.serverSwitcherHidden(true)
        assertEquals(View.GONE, pill.visibility)
    }

    @Test
    fun `a picker request answers with the payload and keeps the pill down`() {
        val activity = launch()
        val pill = activity.switchButton()

        activity.serverPickerRequested()

        val js = shadowOf(activity.webView()).lastEvaluatedJavascript
        assertTrue(
            "picker request must emit the payload, got: $js",
            js != null && js.contains("__omnigentNativeEmitServerPicker"),
        )
        // org.json escapes '/' in the emitted payload (https:\/\/example.com),
        // so match on the field name and host rather than the literal URL.
        assertTrue(
            "payload must carry the pinned origin, got: $js",
            js.contains("currentOrigin") && js.contains("example.com"),
        )

        // Hosting the sidebar picker is proof selection lives in the web UI.
        idlePastWatchdog()
        assertEquals(View.GONE, pill.visibility)
    }

    @Test
    fun `a switch request to an offered server re-points the shell`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://second.example.test")
        val activity = launch() // current: https://example.com, recents carry both

        activity.switchServerRequested("https://second.example.test")

        assertEquals(
            "https://second.example.test",
            shadowOf(activity.webView()).lastLoadedUrl,
        )
        assertEquals(
            "https://second.example.test",
            ServerStore(context).currentServerUrl(),
        )
    }

    @Test
    fun `a switch request to an unlisted URL is refused`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val activity = launch()

        // Page script must not be able to steer the shell to an arbitrary
        // origin: only picker-offered servers are valid switch targets.
        activity.switchServerRequested("https://evil.example.test")

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
        assertEquals("https://example.com", ServerStore(context).currentServerUrl())
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun MainActivity.switchButton(): View =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as View

    private fun MainActivity.serverSwitcherHidden(hidden: Boolean) {
        MainActivity::class
            .java
            .getDeclaredMethod("onServerSwitcherHidden", Boolean::class.javaPrimitiveType)
            .apply { isAccessible = true }
            .invoke(this, hidden)
    }

    private fun MainActivity.serverPickerRequested() {
        MainActivity::class
            .java
            .getDeclaredMethod("onServerPickerRequested")
            .apply { isAccessible = true }
            .invoke(this)
    }

    private fun MainActivity.switchServerRequested(url: String) {
        MainActivity::class
            .java
            .getDeclaredMethod("onSwitchServerRequested", String::class.java)
            .apply { isAccessible = true }
            .invoke(this, url)
    }
}
