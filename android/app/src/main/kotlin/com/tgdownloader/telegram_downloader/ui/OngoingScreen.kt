package com.tgdownloader.telegram_downloader.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.tgdownloader.telegram_downloader.QueueItem
import com.tgdownloader.telegram_downloader.Space
import com.tgdownloader.telegram_downloader.TgViewModel
import com.tgdownloader.telegram_downloader.Type
import com.tgdownloader.telegram_downloader.tgColors
import kotlinx.coroutines.delay

/**
 * The Ongoing destination: the queue, live.
 *
 * A row is a LINE of the list, not a card: the status dot leads it, the
 * title sits above the detail, and a hairline separates the rows.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun OngoingScreen(vm: TgViewModel) {
    val c = tgColors()
    val items = vm.queue

    // The queue is polled while this screen is on show: the worker runs in
    // Python and there is no callback to subscribe to, and the screen is
    // only composed while it is the destination -- so the poll stops as
    // soon as the user leaves.
    LaunchedEffect(Unit) {
        while (true) {
            vm.refreshQueue()
            delay(700)
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(c.page),
    ) {
        // -- the toolbar
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = Space.pagePad, vertical = Space.s3),
            verticalArrangement = Arrangement.spacedBy(Space.s2),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = "Downloads (${items.size})",
                    color = c.ink,
                    fontSize = Type.body,
                    fontWeight = FontWeight.SemiBold,
                )
                Text(
                    text = summary(items),
                    color = c.muted,
                    fontSize = Type.meta,
                    modifier = Modifier
                        .weight(1f)
                        .padding(start = Space.s2),
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            // The buttons wrap instead of running off the screen: a phone
            // is narrower than a desktop window.
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(Space.s2),
                verticalArrangement = Arrangement.spacedBy(Space.s2),
            ) {
                TgButton(
                    text = "Clear finished",
                    kind = TgButtonKind.Quiet,
                    onClick = vm::clearFinished,
                )
                TgButton(
                    text = "Resume all / Start",
                    kind = TgButtonKind.Secondary,
                    onClick = vm::startQueue,
                )
                TgButton(
                    text = "Cancel batch",
                    kind = TgButtonKind.Secondary,
                    onClick = vm::cancelBatch,
                )
            }
        }

        if (items.isEmpty()) {
            EmptyState(onAdd = { vm.goTo(Destination.Add) })
        } else {
            LazyColumn(modifier = Modifier.fillMaxSize()) {
                items(items, key = { it.id }) { item ->
                    QueueRow(item = item, vm = vm)
                }
            }
        }
    }
}

/**
 * The statuses that mean "this item is working right now".
 *
 * A set, not a parenthesised list: `x in ("a", "b")` is not Kotlin. The
 * `in` operator wants a Collection on its right-hand side, and a
 * parenthesised list of strings is just an expression, so the parser
 * rejects it -- and the error it gives ("Expecting ')'") points at the
 * wrong thing entirely.
 */
private val LIVE_STATUSES = setOf("downloading", "converting", "fetching")

/** The statuses that mean "this item did not finish". */
private val FAILED_STATUSES = setOf("error", "cancelled")

/** Only the counters that are non-zero are shown: a zero is not a problem. */
private fun summary(items: List<QueueItem>): String {
    if (items.isEmpty()) return "Nothing here yet."
    val active = items.count { it.status in LIVE_STATUSES }
    val queued = items.count { it.status == "queued" }
    val done = items.count { it.status == "done" }
    val failed = items.count { it.status in FAILED_STATUSES }
    val parts = listOf(
        active to "active",
        queued to "queued",
        done to "done",
        failed to "need attention",
    ).mapNotNull { (n, label) -> if (n > 0) "$n $label" else null }
    return if (parts.isEmpty()) "All clear." else parts.joinToString("  ")
}

@Composable
private fun QueueRow(item: QueueItem, vm: TgViewModel) {
    val c = tgColors()
    val live = item.status in LIVE_STATUSES
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = Space.pagePad, vertical = Space.rowPadV),
    ) {
        Row(verticalAlignment = Alignment.Top) {
            TgStatusDot(item.uiStatus)
            Column(
                modifier = Modifier
                    .weight(1f)
                    .padding(start = Space.s3),
                verticalArrangement = Arrangement.spacedBy(Space.s1),
            ) {
                Text(
                    text = (item.title + item.ext).ifEmpty { item.url },
                    color = c.ink,
                    fontSize = Type.title,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                when {
                    live -> {
                        Text(
                            text = item.detail,
                            color = c.muted,
                            fontSize = Type.meta,
                        )
                        TgProgressBar(
                            value = if (item.status == "fetching") null else item.progress / 100f,
                        )
                    }
                    item.status == "queued" || item.status == "cancelled" -> {
                        Text(
                            text = item.idleLabel,
                            color = c.muted,
                            fontSize = Type.meta,
                        )
                    }
                    item.status == "done" && item.dest.isNotEmpty() -> {
                        Text(
                            text = item.dest,
                            color = c.muted,
                            fontSize = Type.meta,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                    item.status == "error" && item.error.isNotEmpty() -> {
                        Text(
                            text = item.error,
                            color = c.error,
                            fontSize = Type.meta,
                        )
                    }
                    item.status == "done" && item.error.isNotEmpty() -> {
                        // Kept-original note (e.g. ffmpeg missing) -- neutral,
                        // not loud.
                        Text(text = item.error, color = c.muted, fontSize = Type.meta)
                    }
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(Space.s1)) {
                if (item.status in FAILED_STATUSES) {
                    TgButton(
                        text = "Retry",
                        kind = TgButtonKind.Quiet,
                        onClick = { vm.retry(item.id) },
                    )
                }
                if (live) {
                    TgButton(
                        text = "Pause",
                        kind = TgButtonKind.Quiet,
                        onClick = { vm.pause(item.id) },
                    )
                } else {
                    TgButton(
                        text = "Remove",
                        kind = TgButtonKind.Quiet,
                        onClick = { vm.remove(item.id) },
                    )
                }
            }
        }
        TgHairline()
    }
}

/**
 * The empty state: a mark in a disc, a headline, and the one thing that
 * helps.
 */
@Composable
private fun EmptyState(onAdd: () -> Unit) {
    val c = tgColors()
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(Space.s3),
            modifier = Modifier.padding(Space.s6),
        ) {
            Box(
                modifier = Modifier
                    .size(52.dp)
                    .clip(CircleShape)
                    .background(c.well),
                contentAlignment = Alignment.Center,
            ) {
                Text(text = "↓", color = c.outline, fontSize = Type.display)
            }
            Text(
                text = "No downloads in queue",
                color = c.ink,
                fontSize = Type.title,
                fontWeight = FontWeight.SemiBold,
            )
            Text(
                text = "Add links from the Add tab to get started.",
                color = c.muted,
                fontSize = Type.meta,
            )
            TgButton(
                text = "Add downloads",
                kind = TgButtonKind.Primary,
                onClick = onAdd,
            )
        }
    }
}
