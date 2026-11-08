# Use the RuView workspace

Use this workspace to inspect a configured receive-only sensing source and its enabled controls. Check the provider and current status before acting. A control can stay unavailable even when the source reports support.

## Open help

Select **Help** to browse the topics. Select **Start walkthrough** to follow all 14 steps. Contextual **Help** buttons beside workspace sections and command forms open the relevant topic.

Use the search field to find a topic. Use **Previous**, **Next**, or a related topic to navigate. The walkthrough ends with **Finish walkthrough**. Help makes no connection, calibration, recording or deletion request.

Use **Tab** to move through the controls. In the topic list, **Up Arrow**, **Down Arrow**, **Home** and **End** move focus. Activate a topic with **Enter** or **Space**. Press **Escape** while focus is in help to close it and return to the invoking control. Help is a nonmodal region, so focus can also move into the workspace.

Select **Show workspace section** to close help and move keyboard focus to that section. If the section is absent, the button stays unavailable.

## Choose the intended profile

1. Load the configured profile list in the connection controls.
2. Select the intended enabled profile.
3. Connect and wait for a current connected status.
4. Check the selected provider and source mode.

A disabled or missing profile cannot be used here. Ask the person who manages Cyber Controller to configure it. Do not enter an arbitrary service URL, path, password or token in the browser.

Attaching a configured service does not establish that a sensor, model or capability is available.

## Read capability and source labels

**Available** means the source reports the capability available now. The corresponding control must also be enabled.

**Unavailable** means the capability is known but cannot be used now. **Unsupported** means this provider does not offer it. Missing status does not establish support or availability.

RuView upstream and CC-native CSI are separate providers. Switching the viewing provider does not give native CSI the upstream provider's capabilities.

CC-native CSI does not support pose, imaging, through-wall imaging, biometric identification, multi-person separation or metric tracking. Upstream estimates do not remove those limits.

## Distinguish sensing from a scan

This view displays receive-only sensing. It does not scan for networks or devices, identify people, or transmit radio signals.

Check the source mode.

- **Live** describes data reported by the current source.
- **Replay** shows recorded data.
- **Simulation** shows generated data.
- An unknown mode does not establish a live observation.

Pose and vital-sign views, when supplied by RuView upstream, are non-diagnostic estimates. They are not identity, health or accuracy results.

A missing reading is not a zero. A stale reading is not a new observation.

## Choose models deliberately

1. Check the provider and model capability.
2. Choose an existing reported model or configured profile.
3. Use an enabled action.
4. Wait for the reported result and inspect the resulting model status.

An accepted click is not proof that weights loaded or a profile changed. Some load, activation, deletion or training controls can remain unavailable. Help cannot enable them, download weights, browse arbitrary files or create a training-job interface.

## Calibrate the intended setup

1. Confirm the connected source, room setup and sensor selection.
2. Start calibration only when its control is enabled.
3. Wait for the reported calibration state.
4. Finalize or cancel the intended calibration through its own control.

Do not guess missing setup or session details. A restarted service or changed sensor setup can make an earlier calibration unsuitable.

Finalizing, cancelling unfinished calibration and resetting saved calibration are different actions. Read the confirmation before resetting. Calibration does not add an unsupported capability.

## Start and stop the same recording

1. Confirm that the intended upstream source is connected and current.
2. Review the local retention policy and capacity.
3. Start recording once.
4. Wait for the confirmed start result and keep its recording identity.
5. Stop that same recording through its enabled Stop control.
6. Wait for the final result and inspect the retained list.

If a request times out, inspect state before retrying. A retry must keep the original recording identity. Never retarget a delayed stop to whichever recording is active now.

If the screen cannot establish the recording identity, retention policy or safe current state, leave the affected control unavailable. Starting another recording does not resolve an uncertain earlier result.

Stopping a local wait, closing help, disconnecting a view or closing the page is not confirmation that capture stopped.

## Inspect retained recordings

1. Inspect the current reported retained list.
2. Match the recording to its confirmed start and stop results.
3. Check its status before replay or deletion.

An unavailable list is not an empty store. A stale list can describe an earlier state. Refresh before deciding that a recording is missing.

Help does not inspect raw filesystem paths or export recordings.

## Delete one recording manually

1. Inspect the current retained list.
2. Select the exact recording you intend to remove.
3. Read the confirmation.
4. Explicitly confirm that target.
5. Wait for the result and refresh the list.

Deletion is permanent. Do not guess a missing target or delete a recording to resolve an uncertain stop. If the retained identity cannot be established, leave deletion unavailable.

Recordings remain local until explicit manual deletion. There is no automatic capture, export, expiry or deletion.

## Respond to capacity limits

The product limits are 64 retained recordings and 1 GiB of retained data in total. Each recording is bounded by 128 MiB, 100,000 frames and 15 minutes.

The first reached limit can stop or reject capture. A completed recording can report that capacity was reached. A capacity limit never authorizes automatic deletion of an older recording.

Inspect the retained list and manually delete only recordings you no longer need. Confirm each exact target. Repeated Start requests do not make space.

## Recover current state

1. Check the browser connection and workspace status.
2. Refresh and wait for a current provider observation.
3. Inspect the affected operation before repeating it.

Last-known values can remain visible while updates are unavailable. Do not interpret them as current results.

If Cyber Controller requires authentication, sign in again and reload the page. If the sensing source reports an authentication error, check the configured profile with the person who manages it. Do not paste a token into a browser field.

If current state remains unavailable, leave affected actions unavailable. Do not bypass a disabled control or manufacture a missing result.

## Finish and leave

1. Check for a recording or calibration still in progress.
2. Stop the exact confirmed recording, or finish or cancel the intended calibration.
3. Wait for the reported result.
4. Inspect retained recordings if needed.
5. Disconnect the intended upstream profile.
6. Close the workspace.

Disconnecting does not delete recordings or shut down an independently operated sensing service. Closing help only closes help.

## Keep private information private

Use only a source you are authorized to observe. Keep service credentials in private configuration and out of browser content, screenshots and shared logs.

The help feature stores no walkthrough progress, profile choice or search text in browser storage. It sends no telemetry or product commands. Closing the workspace removes its help interface and listeners.

