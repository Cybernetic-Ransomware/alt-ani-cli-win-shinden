package pl.altani.spike.offline.background

/** Which runtime a session drives; [CONTROLLED] exists in debug builds only (controlled server, own database). */
enum class RunTarget { APP, CONTROLLED }

/** Only [USER] changes a decision; JobScheduler documents the stop reason as diagnostic otherwise. */
enum class SystemStop { USER, CONNECTIVITY, OTHER }

/** Called from the coordinator's threads; never again for a generation after [finish] or a system stop. */
interface RunHost {
    val path: RunPath

    fun show(generation: Long, model: NotificationModel)

    /** Keeps the CPU awake for real transfer work; a no-op where the system already holds a lock for the job. */
    fun setWorking(working: Boolean)

    fun finish(reschedule: Boolean)
}

/** What a launch carries to the component; [initiatedAtMs] is `elapsedRealtime` of the user's decision. */
data class LaunchSpec(val target: RunTarget, val scope: NetworkScope, val initiatedAtMs: Long, val estimatedBytes: Long?) {
    fun extras(): Map<String, Any> =
        mapOf(KEY_TARGET to target.name, KEY_SCOPE to scope.name, KEY_INITIATED_AT to initiatedAtMs)

    companion object {
        const val KEY_TARGET = "target"
        const val KEY_SCOPE = "network_scope"
        const val KEY_INITIATED_AT = "initiated_at"

        /** Unknown or missing values fall back to the stricter choice: the real runtime, Wi-Fi only, an expired window. */
        fun fromExtras(get: (String) -> Any?): LaunchSpec =
            LaunchSpec(
                target = RunTarget.entries.firstOrNull { it.name == get(KEY_TARGET) } ?: RunTarget.APP,
                scope = NetworkScope.entries.firstOrNull { it.name == get(KEY_SCOPE) } ?: NetworkScope.WIFI,
                initiatedAtMs = (get(KEY_INITIATED_AT) as? Long) ?: Long.MIN_VALUE / 2,
                estimatedBytes = null,
            )
    }
}

interface RunLauncher {
    /** Null below API 30, where the file store refuses offline downloads. */
    val path: RunPath?

    /** A component is scheduled or running; a second launch would replace or duplicate it. */
    fun isActive(): Boolean

    /** Must be called while the app is visible; false when the system refuses. */
    fun launch(spec: LaunchSpec): Boolean
}

interface AttentionNotifier {
    fun post(reason: AttentionReason)

    fun clear()
}
