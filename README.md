# OpenChamber Voice

Windows application for Russian dictation into the focused OpenChamber window. Run `Start.cmd`, click **Включить микрофон**, focus the desired chat and place the cursor in its input box, say «приложение» and dictate. After 4.5 seconds of silence it copies, pastes, and presses Enter. If you say the wake word alone, it waits for the next speech segment. Click the button again or close the window to stop listening.

Audio is processed locally with faster-whisper `small`; the first start downloads the model. Text is sent to the active chat and its model provider. Speech recognition and voice activity detection can mistake other voices, speaker output or silence for speech; this tool is not speaker identification. Use headphones and stop it for private conversations. The process-name check cannot prove the chat input is focused or which chat is selected: verify both yourself. The clipboard is replaced. This initial version is not verified against live speech; long pauses can split a message and text in progress is discarded on exit.

Requirements: Python, `pip install faster-whisper numpy sounddevice webrtcvad-wheels`.
