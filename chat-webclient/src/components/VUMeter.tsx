// Updates a CSS custom property directly on the DOM node (no React re-renders)
// so the animation runs at full frame-rate without touching the React tree.

import { useEffect, useRef } from 'react'

export function VUMeter({ stream }: { stream: MediaStream | null }) {
  const barRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const bar = barRef.current
    if (!bar) return

    if (!stream || stream.getAudioTracks().filter((t) => t.readyState === 'live').length === 0) {
      bar.style.setProperty('--vu', '0')
      return
    }

    let ctx: AudioContext | null = null
    let rafId = 0

    try {
      ctx = new AudioContext()
      void ctx.resume()
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      analyser.smoothingTimeConstant = 0.6
      ctx.createMediaStreamSource(stream).connect(analyser)
      const data = new Uint8Array(analyser.frequencyBinCount)

      const tick = () => {
        analyser.getByteTimeDomainData(data)
        let sum = 0
        for (const v of data) {
          const n = (v - 128) / 128
          sum += n * n
        }
        const rms = Math.sqrt(sum / data.length)
        bar.style.setProperty('--vu', String(Math.min(1, rms * 6)))
        rafId = requestAnimationFrame(tick)
      }
      rafId = requestAnimationFrame(tick)
    } catch {
      // AudioContext unavailable (e.g. sandboxed iframe)
    }

    return () => {
      cancelAnimationFrame(rafId)
      ctx?.close().catch(() => {})
      bar.style.setProperty('--vu', '0')
    }
  }, [stream])

  return <div className="tui-vu-bar" ref={barRef} />
}
