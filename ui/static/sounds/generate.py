"""Generate placeholder WAV audio files for development."""
import math, struct, wave, random

SAMPLE_RATE = 22050
MAX_AMP = 16000

def write_wav(path, samples):
    with wave.open(path, 'w') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for s in samples:
            s = max(-32768, min(32767, int(s)))
            w.writeframes(struct.pack('<h', s))

def sine(freq, dur_sec, amp=MAX_AMP):
    n = int(SAMPLE_RATE * dur_sec)
    return [amp * math.sin(2 * math.pi * freq * i / SAMPLE_RATE) for i in range(n)]

def envelope(samples, attack=0.01, decay=0.1):
    n = len(samples)
    a = int(SAMPLE_RATE * attack)
    d = int(SAMPLE_RATE * decay)
    for i in range(min(a, n)):
        samples[i] *= i / a
    for i in range(max(0, n-d), n):
        samples[i] *= (n - i) / d
    return samples

# ── tak.wav — short percussive click ──
tak = envelope(sine(600, 0.08), attack=0.001, decay=0.05)
write_wav(r'ui/static/sounds/tak.wav', tak)

# ── tea-clink.wav — short bell-like ring ──
clink = [0.0] * int(SAMPLE_RATE * 0.5)
for freq, amp, dur in [(880, 1.0, 0.15), (1320, 0.3, 0.1), (1760, 0.15, 0.08)]:
    n = int(SAMPLE_RATE * dur)
    seg = [amp * math.sin(2 * math.pi * freq * i / SAMPLE_RATE) for i in range(n)]
    seg = envelope(seg, attack=0.002, decay=0.15)
    for i in range(min(n, len(clink))):
        clink[i] += seg[i]
max_c = max(abs(s) for s in clink) or 1
clink = [s / max_c * MAX_AMP * 0.6 for s in clink]
write_wav(r'ui/static/sounds/tea-clink.wav', clink)

# ── cafe-chatter.wav — filtered noise ──
random.seed(42)
noise = [random.uniform(-1, 1) for _ in range(SAMPLE_RATE * 10)]
for _ in range(8):
    noise = [sum(noise[max(0,i-3):i+1])/(i+1-max(0,i-3)) for i in range(len(noise))]
max_n = max(abs(s) for s in noise) or 1
cafe = [s / max_n * MAX_AMP * 0.3 for s in noise]
write_wav(r'ui/static/sounds/cafe-chatter.wav', cafe)

# ── oud.wav — low drone with slow modulation ──
oud_samples = []
for i in range(SAMPLE_RATE * 10):
    t = i / SAMPLE_RATE
    mod = 1 + 0.15 * math.sin(2 * math.pi * 0.25 * t)
    s = mod * (0.7 * math.sin(2 * math.pi * 110 * t) + 0.3 * math.sin(2 * math.pi * 165 * t))
    oud_samples.append(s * MAX_AMP * 0.2)
write_wav(r'ui/static/sounds/oud.wav', oud_samples)

# ── card-play.wav — soft papery "thwip" ──
play_snd = envelope(sine(400, 0.1), attack=0.002, decay=0.08)
play_snd = [s * 0.7 for s in play_snd]
write_wav(r'ui/static/sounds/card-play.wav', play_snd)

# ── card-capture.wav — low "thump" ──
cap = [0.0] * int(SAMPLE_RATE * 0.25)
for freq, amp in [(200, 0.8), (300, 0.4)]:
    seg = sine(freq, 0.2, amp=int(MAX_AMP*amp))
    seg = envelope(seg, attack=0.005, decay=0.15)
    for i in range(min(len(seg), len(cap))):
        cap[i] += seg[i]
write_wav(r'ui/static/sounds/card-capture.wav', cap)

# ── card-shuffle.wav — rustling noise ──
random.seed(99)
shuf = []
for _ in range(int(SAMPLE_RATE * 1.2)):
    shuf.append(random.uniform(-1, 1))
# amplitude modulation to simulate bursts
for i in range(len(shuf)):
    t = i / SAMPLE_RATE
    shuf[i] *= 0.5 + 0.5 * math.sin(2 * math.pi * 8 * t)
shuf = envelope(shuf, attack=0.02, decay=0.3)
max_s = max(abs(s) for s in shuf) or 1
shuf = [s / max_s * MAX_AMP * 0.25 for s in shuf]
write_wav(r'ui/static/sounds/card-shuffle.wav', shuf)

# ── card-flip.wav — quick "flip" frequency sweep ──
flip = []
for i in range(int(SAMPLE_RATE * 0.08)):
    t = i / SAMPLE_RATE
    freq = 400 + 600 * (t / 0.08)  # sweep 400→1000 Hz
    flip.append(math.sin(2 * math.pi * freq * t) * MAX_AMP * 0.5)
flip = envelope(flip, attack=0.003, decay=0.05)
write_wav(r'ui/static/sounds/card-flip.wav', flip)

print("Done — 8 WAV files created in ui/static/sounds/")
