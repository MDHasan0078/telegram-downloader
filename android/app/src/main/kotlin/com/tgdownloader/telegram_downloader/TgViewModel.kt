package com.tgdownloader.telegram_downloader

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.tgdownloader.telegram_downloader.ui.Destination
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** A transient message at the bottom of the screen. */
data class Notice(
    val text: String,
    val isError: Boolean = false,
    val id: Long = 0L,
)

/**
 * The screen's state, and every call into the engine.
 *
 * The engine is Python and the calls are slow (a network round-trip, a
 * queue read, a folder check). Every one of them goes through `Bridge`,
 * which hops to a background dispatcher, so the UI thread is never the one
 * waiting. Nothing here blocks: a button that awaits something shows a
 * spinner instead of its label while it does.
 */
class TgViewModel(app: Application) : AndroidViewModel(app) {

    var destination by mutableStateOf(Destination.Add)
        private set

    var settings by mutableStateOf(AppSettings())
        private set

    var queue by mutableStateOf<List<QueueItem>>(emptyList())
        private set

    var dependencies by mutableStateOf<List<DependencyRow>>(emptyList())
        private set

    var capabilities by mutableStateOf(Capabilities())
        private set

    var notice by mutableStateOf<Notice?>(null)
        private set

    // -- the Add screen ----------------------------------------------------

    var linksText by mutableStateOf("")
        private set

    var detectedCount by mutableStateOf(0)
        private set

    var statusText by mutableStateOf("")
        private set

    var adding by mutableStateOf(false)
        private set

    // -- the busy flags of the other screens -------------------------------

    var authBusy by mutableStateOf(false)
        private set

    var depsBusy by mutableStateOf(false)
        private set

    var updateBusy by mutableStateOf(false)
        private set

    private var parseJob: Job? = null
    private var noticeId = 0L

    val dark: Boolean get() = settings.theme != "light"

    init {
        refreshAll()
    }

    // -- helpers -----------------------------------------------------------

    private fun say(text: String, isError: Boolean = false) {
        noticeId += 1
        notice = Notice(text, isError, noticeId)
    }

    fun clearNotice() {
        notice = null
    }

    private fun guard(): Boolean = try {
        Bridge.ensureInitialized(getApplication())
        true
    } catch (e: Exception) {
        say(e.message ?: "The engine could not start.", isError = true)
        false
    }

    // -- loading -----------------------------------------------------------

    fun refreshAll() {
        if (!guard()) return
        viewModelScope.launch {
            runCatching { Bridge.settings() }
                .onSuccess { settings = it }
                .onFailure { say(it.message ?: "Could not read the settings.", true) }
            runCatching { Bridge.capabilities() }
                .onSuccess { caps ->
                    capabilities = caps
                    // Fast and Max need ffmpeg, which Android does not have.
                    // A stored mode of 2 or 3 would fail at the very end of a
                    // long download, so the app falls back to Original.
                    if (!caps.ffmpeg && settings.outputMode != "1") {
                        setOutputMode("1")
                    }
                }
            runCatching { Bridge.queueItems() }
                .onSuccess { queue = it }
            // The app opens where the user left off: without credentials
            // there is nothing to add, so Account auth is the only useful
            // screen.
            if (!settings.hasApi && destination == Destination.Add) {
                destination = Destination.Auth
            }
        }
    }

    fun refreshQueue() {
        if (!guard()) return
        viewModelScope.launch {
            runCatching { Bridge.queueItems() }.onSuccess { queue = it }
        }
    }

    fun refreshDependencies() {
        if (!guard()) return
        depsBusy = true
        viewModelScope.launch {
            runCatching { Bridge.dependencies() }
                .onSuccess { dependencies = it }
                .onFailure { say(it.message ?: "Could not check the dependencies.", true) }
            depsBusy = false
        }
    }

    fun goTo(destination: Destination) {
        this.destination = destination
        when (destination) {
            Destination.Ongoing -> refreshQueue()
            Destination.Settings -> refreshDependencies()
            Destination.Auth -> refreshAll()
            Destination.Add -> Unit
        }
    }

    // -- the theme ---------------------------------------------------------

    fun toggleTheme() {
        val next = if (dark) "light" else "dark"
        settings = settings.copy(theme = next)
        viewModelScope.launch {
            runCatching { Bridge.setTheme(next) }
                .onFailure { say(it.message ?: "Could not save the theme.", true) }
        }
    }

    // -- the Add screen ----------------------------------------------------

    /**
     * The URL counter.
     *
     * The split is the ENGINE's, not a second copy of it, so the counter
     * cannot disagree with what gets queued. It is debounced: the call is
     * off the UI thread and the user is still typing.
     */
    fun onLinksChanged(text: String) {
        linksText = text
        parseJob?.cancel()
        parseJob = viewModelScope.launch {
            delay(250)
            runCatching { Bridge.parseLinks(text) }
                .onSuccess { detectedCount = it.size }
        }
    }

    fun clearLinks() {
        linksText = ""
        detectedCount = 0
        statusText = ""
    }

    /** The .txt batch file was read: fill the field with what it holds. */
    fun setLinksFromFile(text: String) {
        viewModelScope.launch {
            val urls = runCatching { Bridge.parseLinks(text) }.getOrDefault(emptyList())
            linksText = urls.joinToString("\n")
            detectedCount = urls.size
            say("Loaded ${urls.size} URL${if (urls.size == 1) "" else "s"} from file.")
        }
    }

    /** The batch file could not be read. */
    fun reportFileError(message: String) {
        say(message, isError = true)
    }

    /** The folder the user picked cannot be used. */
    fun reportFolderError(message: String) {
        say(message, isError = true)
    }

    fun addLinks() {
        if (adding) return
        if (!guard()) return
        adding = true
        statusText = "Adding…"
        viewModelScope.launch {
            try {
                val urls = Bridge.parseLinks(linksText)
                if (urls.isEmpty()) {
                    statusText = ""
                    say("Paste at least one t.me link first.", isError = true)
                    return@launch
                }
                val result = Bridge.addLinks(urls)
                if (result.ok) {
                    statusText = ""
                    linksText = ""
                    detectedCount = 0
                    say("Downloading ${result.added} link${if (result.added == 1) "" else "s"}.")
                    destination = Destination.Ongoing
                } else {
                    statusText = ""
                    say(result.error.ifEmpty { "Could not queue the links." }, isError = true)
                    if (result.error.contains("Account auth")) {
                        destination = Destination.Auth
                    }
                }
            } catch (e: Exception) {
                statusText = ""
                say(e.message ?: "Could not queue the links.", isError = true)
            } finally {
                adding = false
            }
        }
    }

    fun setOutputMode(mode: String) {
        settings = settings.copy(outputMode = mode)
        viewModelScope.launch {
            runCatching { Bridge.setOutputMode(mode) }
                .onFailure { say(it.message ?: "Could not save the output mode.", true) }
        }
    }

    // -- the download folder -----------------------------------------------

    fun changeDownloadDir(path: String) {
        if (!guard()) return
        viewModelScope.launch {
            val error = Bridge.setDownloadDir(path)
            if (error.isEmpty()) {
                say("Download folder updated.")
                refreshAll()
            } else {
                say(error, isError = true)
            }
        }
    }

    // -- the queue ---------------------------------------------------------

    fun retry(id: String) {
        viewModelScope.launch {
            runCatching { Bridge.retryItem(id) }
                .onFailure { say(it.message ?: "Could not retry the download.", true) }
            refreshQueue()
        }
    }

    fun remove(id: String) {
        viewModelScope.launch {
            runCatching { Bridge.removeItem(id) }
                .onFailure { say(it.message ?: "Could not remove the download.", true) }
            refreshQueue()
        }
    }

    /** Pause a running download. It resumes from where it stopped. */
    fun pause(id: String) {
        viewModelScope.launch {
            runCatching { Bridge.pauseItem(id) }
                .onFailure { say(it.message ?: "Could not pause the download.", true) }
            say("Pausing — the partial file is kept, so it resumes where it stopped.")
            refreshQueue()
        }
    }

    fun clearFinished() {
        viewModelScope.launch {
            runCatching { Bridge.clearFinished() }
                .onFailure { say(it.message ?: "Could not clear the queue.", true) }
            refreshQueue()
        }
    }

    fun startQueue() {
        viewModelScope.launch {
            runCatching { Bridge.startQueue() }
                .onFailure { say(it.message ?: "Could not start the queue.", true) }
            refreshQueue()
        }
    }

    fun cancelBatch() {
        viewModelScope.launch {
            runCatching { Bridge.cancelBatch() }
            say("Stopping — the current file is cancelled, the rest stay queued.")
            refreshQueue()
        }
    }

    // -- the account -------------------------------------------------------

    fun saveCredentials(apiId: String, apiHash: String) {
        if (!guard()) return
        authBusy = true
        viewModelScope.launch {
            try {
                Bridge.setApiCredentials(apiId, apiHash)
                say("Credentials saved.")
                refreshAll()
            } catch (e: Exception) {
                say(e.message ?: "Could not save the credentials.", isError = true)
            } finally {
                authBusy = false
            }
        }
    }

    fun verifyCredentials() {
        if (!guard()) return
        authBusy = true
        viewModelScope.launch {
            try {
                val result = Bridge.verifyCredentials()
                say(result.message, isError = !result.ok)
            } catch (e: Exception) {
                say(e.message ?: "Could not verify the credentials.", isError = true)
            } finally {
                authBusy = false
            }
        }
    }

    fun loginBegin(phone: String) {
        if (!guard()) return
        authBusy = true
        viewModelScope.launch {
            try {
                val state = Bridge.loginBegin(phone)
                loginState = state.state
                if (!state.ok) say(state.error.ifEmpty { "Login failed." }, isError = true)
            } catch (e: Exception) {
                say(e.message ?: "Login failed.", isError = true)
            } finally {
                authBusy = false
            }
        }
    }

    fun loginCode(code: String) {
        if (!guard()) return
        authBusy = true
        viewModelScope.launch {
            try {
                val state = Bridge.loginCode(code)
                loginState = state.state
                if (!state.ok) say(state.error.ifEmpty { "Login failed." }, isError = true)
                else if (state.state == "done") say("Login OK.")
            } catch (e: Exception) {
                say(e.message ?: "Login failed.", isError = true)
            } finally {
                authBusy = false
            }
        }
    }

    fun loginPassword(password: String) {
        if (!guard()) return
        authBusy = true
        viewModelScope.launch {
            try {
                val state = Bridge.loginPassword(password)
                loginState = state.state
                if (!state.ok) say(state.error.ifEmpty { "Login failed." }, isError = true)
                else say("Login OK.")
            } catch (e: Exception) {
                say(e.message ?: "Login failed.", isError = true)
            } finally {
                authBusy = false
            }
        }
    }

    /** Where the login is: "" (idle), "code", "password" or "done". */
    var loginState by mutableStateOf("")
        private set

    fun logout() {
        if (!guard()) return
        authBusy = true
        viewModelScope.launch {
            try {
                val serverOk = Bridge.logout()
                say(
                    if (serverOk) {
                        "Logged out — the server session was revoked and the files removed."
                    } else {
                        "The local session was removed, but the server logout FAILED. " +
                            "Revoke the session at my.telegram.org if the device is shared."
                    },
                    isError = !serverOk,
                )
                refreshAll()
            } catch (e: Exception) {
                say(e.message ?: "Could not log out.", isError = true)
            } finally {
                authBusy = false
            }
        }
    }

    // -- the settings screen ------------------------------------------------

    fun checkForUpdate() {
        if (!guard()) return
        updateBusy = true
        viewModelScope.launch {
            try {
                val result = Bridge.checkForUpdate()
                if (!result.ok) {
                    say(result.error.ifEmpty { "Could not reach the update server." }, isError = true)
                } else if (!result.newer) {
                    say("You are up to date (v${result.current}).")
                } else {
                    say("Telegram Downloader ${result.version} is available (you have ${result.current}).")
                }
            } catch (e: Exception) {
                say(e.message ?: "Update check failed.", isError = true)
            } finally {
                updateBusy = false
            }
        }
    }

    fun resetEverything() {
        if (!guard()) return
        viewModelScope.launch {
            try {
                Bridge.resetEverything()
                say("Reset complete. Re-enter your API credentials.")
                destination = Destination.Auth
                refreshAll()
            } catch (e: Exception) {
                say(e.message ?: "Could not reset everything.", isError = true)
            }
        }
    }
}
