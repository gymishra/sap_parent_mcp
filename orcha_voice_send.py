"""
🎙️ Orcha Voice Auto-Send Script
================================
Records your voice → Transcribes → Pastes into Orcha chat → Auto-sends

SETUP (run once):
    pip install SpeechRecognition sounddevice numpy pyautogui pyperclip keyboard vosk

HOW TO USE:
    1. Run this script: python orcha_voice_send.py
    2. Click ONCE inside the Orcha chat input box
    3. Hold SPACE to record your voice
    4. Release SPACE — it transcribes and auto-sends!
    5. Press ESC to quit

REQUIREMENTS:
    - Windows 10/11
    - Microphone
    - Orcha chat window open
    - pip install SpeechRecognition sounddevice numpy pyautogui pyperclip keyboard vosk
"""

import speech_recognition as sr
import sounddevice as sd
import numpy as np
import pyautogui
import pyperclip
import keyboard
import threading
import time
import sys
import json
import io
import wave

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

HOLD_KEY         = "space"      # Hold this key to record
QUIT_KEY         = "esc"        # Press this to quit
LANGUAGE         = "en-US"      # Speech recognition language
PAUSE_BEFORE     = 0.3          # Seconds to wait before sending
SEND_KEY         = "enter"      # Key to press to send message
SAMPLE_RATE      = 16000        # Will be overridden by detected mic rate
CHANNELS         = 1            # Mono audio

# ──────────────────────────────────────────────
# COLORS FOR TERMINAL OUTPUT
# ──────────────────────────────────────────────

def green(text):  return f"\033[92m{text}\033[0m"
def yellow(text): return f"\033[93m{text}\033[0m"
def red(text):    return f"\033[91m{text}\033[0m"
def cyan(text):   return f"\033[96m{text}\033[0m"
def bold(text):   return f"\033[1m{text}\033[0m"

# ──────────────────────────────────────────────
# FIND A WORKING MICROPHONE
# ──────────────────────────────────────────────

def find_working_mic():
    """Auto-detect a working input device using sounddevice."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            rate = int(d["default_samplerate"])
            for ch in [1, 2]:
                try:
                    audio = sd.rec(int(rate * 0.1), samplerate=rate, channels=ch, dtype="int16", device=i)
                    sd.wait()
                    return i, rate, ch, d["name"]
                except Exception:
                    pass
    return None, None, None, None

# ──────────────────────────────────────────────
# VOICE RECORDING & TRANSCRIPTION
# ──────────────────────────────────────────────

recognizer = sr.Recognizer()
is_recording = False
audio_data   = None
mic_device   = None
mic_rate     = None
mic_channels = None


def record_audio():
    """Record audio while SPACE is held down using sounddevice."""
    global is_recording, audio_data

    print(cyan("\n🎙️  Recording... (release SPACE to stop)"))

    frames = []
    try:
        def callback(indata, frame_count, time_info, status):
            if is_recording:
                frames.append(indata.copy())

        with sd.InputStream(samplerate=mic_rate, channels=mic_channels,
                            dtype="int16", device=mic_device, callback=callback,
                            blocksize=1024):
            while is_recording:
                time.sleep(0.05)
    except Exception as e:
        print(red(f"⚠️  Recording error: {e}"))
        audio_data = None
        return

    if frames:
        raw = np.concatenate(frames, axis=0)
        # Convert to mono if stereo
        if raw.ndim > 1 and raw.shape[1] > 1:
            raw = raw[:, 0]
        raw = raw.flatten()

        # Convert numpy array to WAV bytes, then to sr.AudioData
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(mic_rate)
            wf.writeframes(raw.tobytes())
        buf.seek(0)

        audio_data = sr.AudioData(raw.tobytes(), mic_rate, 2)
        print(green("✅ Recording complete!"))
    else:
        audio_data = None
        print(red("⚠️  No audio captured."))


def transcribe_and_send():
    """Transcribe audio and paste + send in Orcha."""
    global audio_data

    if audio_data is None:
        print(red("⚠️  No audio to transcribe."))
        return

    print(yellow("🔄  Transcribing..."))

    try:
        # Use Vosk (offline, reliable)
        result_json = recognizer.recognize_vosk(audio_data)
        result = json.loads(result_json)
        text = result.get("text", "")
        print(green(f'📝  Transcribed: "{text}"'))

        if text.strip():
            # Small pause to ensure Orcha window is focused
            time.sleep(PAUSE_BEFORE)

            # Copy text to clipboard
            pyperclip.copy(text)

            # Paste into the active text field (Orcha chat box)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)

            # Press ENTER to send
            pyautogui.press(SEND_KEY)
            print(green(f'🚀  Sent: "{text}"'))

        else:
            print(yellow("⚠️  Transcription was empty, nothing sent."))

    except sr.UnknownValueError:
        print(red("❌  Could not understand audio. Please speak clearly."))
    except sr.RequestError as e:
        print(red(f"❌  Speech service error: {e}"))
        print(yellow("    💡 Check your internet connection."))
    except Exception as e:
        print(red(f"❌  Error: {e}"))

    # Reset audio
    audio_data = None


# ──────────────────────────────────────────────
# KEY LISTENERS
# ──────────────────────────────────────────────

recording_thread = None


def on_space_press(event):
    """Start recording when SPACE is pressed."""
    global is_recording, recording_thread

    if not is_recording:
        is_recording = True
        recording_thread = threading.Thread(target=record_audio, daemon=True)
        recording_thread.start()


def on_space_release(event):
    """Stop recording when SPACE is released."""
    global is_recording, recording_thread

    if is_recording:
        is_recording = False

        # Wait for recording thread to finish
        if recording_thread:
            recording_thread.join(timeout=2)

        # Transcribe and send in background
        send_thread = threading.Thread(target=transcribe_and_send, daemon=True)
        send_thread.start()


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    global mic_device, mic_rate, mic_channels

    print(bold(cyan("""
╔══════════════════════════════════════════════╗
║   🎙️  Orcha Voice Auto-Send                  ║
║   ─────────────────────────────────────────  ║
║   HOLD [SPACE]  →  Speak  →  Auto-Send       ║
║   Press [ESC]   →  Quit                      ║
╚══════════════════════════════════════════════╝
    """)))

    # Auto-detect working microphone
    print(yellow("🔍  Detecting microphone..."))
    mic_device, mic_rate, mic_channels, mic_name = find_working_mic()

    if mic_device is None:
        print(red("❌  No working microphone found!"))
        print(yellow("    💡 Check Windows Settings > Privacy > Microphone"))
        print(yellow("    💡 Make sure a mic is connected and enabled"))
        sys.exit(1)

    print(green(f"🎤  Using: {mic_name} (device #{mic_device}, {mic_rate}Hz)"))

    print(yellow("\n📋  INSTRUCTIONS:"))
    print("   1. Click ONCE inside the Orcha chat box")
    print("   2. Come back to this terminal")
    print("   3. Hold SPACE to record")
    print("   4. Release SPACE to transcribe & send")
    print("   5. Press ESC to quit\n")

    print(green("✅  Ready! Waiting for your voice...\n"))

    # Register key hooks
    keyboard.on_press_key(HOLD_KEY,   on_space_press)
    keyboard.on_release_key(HOLD_KEY, on_space_release)

    # Wait for ESC to quit
    keyboard.wait(QUIT_KEY)

    print(yellow("\n👋  Orcha Voice Send stopped. Goodbye!"))
    sys.exit(0)


if __name__ == "__main__":
    main()
