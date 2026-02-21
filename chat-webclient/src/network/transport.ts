export interface SyncTransport {
  connect(circleId: string): Promise<void>
  disconnect(): Promise<void>
  syncOnce(circleId: string): Promise<void>
}

export class StubRelayTransport implements SyncTransport {
  async connect(circleId: string): Promise<void> {
    console.info('[transport] connect (stub)', { circleId })
  }

  async disconnect(): Promise<void> {
    console.info('[transport] disconnect (stub)')
  }

  async syncOnce(circleId: string): Promise<void> {
    console.info('[transport] syncOnce (stub)', { circleId })
  }
}
