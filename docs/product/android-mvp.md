# Android MVP — product requirements

Status: accepted (stage 0 closed, 2026-11). This document records decisions; it does not define screen layouts.
Anything about layout, navigation or visual design not written here is not yet decided.

## Architecture and scope

- A standalone APK. No server is required to run the application's logic.
- Kotlin + Compose for the UI, the shared Python engine through Chaquopy, Media3/ExoPlayer for playback.
- Full offline use is part of the MVP: a persistent download queue, resuming, background downloading, network
  restrictions, a library of downloaded episodes and a separate export.
- History is local and kept per device. Synchronisation (including Turso) is outside the MVP.
- Watching a whole video inside a WebView of the host page, and picture-in-picture, are outside the MVP.

## Watching and history

- The last opened episode, the playback position and the watched status are three separate things.
- An episode is marked watched automatically only when playback reaches its end — there is no percentage
  threshold. The status can also be changed by hand.
- "Resume" (Wznów) starts from the known position. "Episodes" (Odcinki) opens the episode list at the last opened
  episode, with earlier episodes available above it.
- Opening the source page in an external browser is an emergency feature of the MVP. When the source address is not
  known, the episode's Shinden page is opened instead.
- Leaving for the browser updates the last opening, but does not mark the episode watched and does not clear the
  known position. On return there is no mandatory "did you watch it?" question.
- Progress belongs to the episode, not to the host: it stays available after switching the source or moving to a
  local copy.
- After an episode ends, "Next episode" is offered — no autoplay. A matching local copy is preferred.
- "Next" follows the known episode order, offline too. A later episode that has a local copy is offered separately,
  never as the next episode.
- A watched episode keeps its real position and status when opened again; starting from the beginning is the user's
  own action.
- Leaving for another app or locking the screen pauses the video and saves the position.
- Rotation keeps playing, stepping back 1–2 seconds. This is player behaviour, not a rule that lowers the position
  stored in the database.

## Preferences and network

- Audio, subtitle and quality preferences are shared by watching and downloading.
- The default quality is the best available. Criteria can be changed for a single use.
- A chosen quality is an upper limit ("at most 720p"), not an exact value: the best matching variant within the limit
  is chosen; when only higher variants exist there is no match; a variant of unknown height never satisfies a limit;
  the default "best" has no limit.
- The language is never changed automatically when no matching source exists.
- A download job keeps the criteria it was added with, even if the preferences change later.
- Downloading is allowed on Wi-Fi only by default. Losing Wi-Fi suspends the transfer; its return makes resuming
  possible. A network change never lifts a manual pause.
- Mobile data can be allowed for a single job, or by changing the global setting.
- While watching, after switching to mobile data the user is asked for consent for the current episode — not for every
  fragment. The consent does not carry over to the next episode nor to download jobs.
  It covers the current viewing of the episode: it survives a source change and rotation and ends when the viewing
  ends or another episode starts.
- Video that is already buffered may play without consent for new transfers. The player buffers ahead.
- FlareSolverr is optional on Wi-Fi and skipped on mobile data.

## Downloading and interaction

- One episode, several, a range, or "unwatched" can be added, with a preview list that can be corrected.
  "Unwatched" means every episode not marked watched, including gaps before a later watched one. Adding commits
  exactly the corrected preview with the criteria it showed.
- Episodes already downloaded or already in the queue are skipped. The queue order can be changed.
- One active download by default.
- Resuming is first attempted from the same source, refreshing its URL if needed.
- When resuming is impossible, the job waits for a decision. Automatically trying compatible alternatives is an
  option, off by default.
- Changing the source may require starting from the beginning. Incompatible fragments are never joined.
- Interaction with a host happens only when necessary, without pasting cookies by hand.
- With automatic selection an allowed alternative may be tried; with manual selection the user gets "Continue" or
  another host. Choices made after an interaction are kept.
- A job waiting for interaction does not block the rest of the queue.
- A WebView used to pass an interaction and return to the native player is a separate feature from watching a whole
  video in a WebView.

## Library

- Files stay until removed by hand, one by one or in bulk.
- Removing a file does not erase watch history or the position.
- Export makes an independent copy, which removing the file from the library does not affect.
- Running out of space suspends the job with a clear reason. Other files are never deleted automatically and the job
  is not retried endlessly.

## Roadmap

| Stage | Content | Needs Android SDK |
|---|---|---|
| 0 | Requirements agreed (this document) | no |
| 1 | Data model and durable state ([ADR-0004](../adr/0004-local-state-model.md)) | no |
| 2 | Queue execution and offline library logic on the desktop | no |
| 3 | Complete mobile client contract and bridge extension ([ADR-0006](../adr/0006-mobile-client-contract.md)) | no |
| 4 | Reproducible APK build, native dependencies, first playback on Android | yes |
| 5 | Mobile screens, online watching, handling interactions | yes |
| 6 | Offline integration with Android mechanisms, closing the MVP | yes |

Desktop tests of stages 1–3 do not confirm that background work behaves correctly on Android.

## Relation to earlier ADRs

[ADR-0003](../adr/0003-android-feasibility.md) left mobile downloads and Compose screens out of scope. That
exclusion applied to the feasibility experiment and to the core/CLI split, not to the Android MVP, which includes
both as described above.
