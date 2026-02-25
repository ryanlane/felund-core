import { test, expect, type Page } from '@playwright/test'

const RELAY_BASE = process.env.FELUND_RELAY_BASE || 'http://127.0.0.1:8765'

const waitForRemoteAudio = async (page: Page) => {
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid="call-remote-audio"]') as HTMLAudioElement | null
    if (!el || !el.srcObject) return false
    return el.srcObject.getTracks().length > 0
  })
}

const waitForRemoteVideo = async (page: Page) => {
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid="call-remote-video"]') as HTMLVideoElement | null
    if (!el || !el.srcObject) return false
    return el.srcObject.getTracks().length > 0
  })
}

const setupHost = async (page: Page): Promise<string> => {
  await page.goto('/')
  await page.getByTestId('setup-display-name').fill('Host')
  await page.getByTestId('setup-circle-name').fill('e2e-call')
  await page.getByTestId('setup-rendezvous').fill(RELAY_BASE)
  await page.getByTestId('setup-submit').click()

  await page.getByTestId('footer-invite').click()
  await expect(page.getByTestId('invite-code')).toBeVisible()
  const inviteCode = (await page.getByTestId('invite-code').innerText()).trim()
  await page.getByRole('button', { name: 'Close' }).click()
  return inviteCode
}

const setupJoiner = async (page: Page, inviteCode: string): Promise<void> => {
  await page.goto('/')
  await page.getByTestId('setup-tab-join').click()
  await page.getByTestId('setup-invite-code').fill(inviteCode)
  await page.getByTestId('setup-rendezvous').fill(RELAY_BASE)
  await page.getByTestId('setup-submit').click()
}

const startCall = async (page: Page): Promise<void> => {
  await page.getByTestId('footer-call').click()
  await page.getByTestId('call-start').click()
  await expect(page.getByTestId('call-mute')).toBeVisible()
}

const joinCall = async (page: Page): Promise<void> => {
  await page.getByTestId('footer-call').click()
  await page.getByTestId('call-join').click()
  await expect(page.getByTestId('call-mute')).toBeVisible()
}

test('audio call delivers remote media', async ({ browser }) => {
  const hostContext = await browser.newContext({ permissions: ['microphone', 'camera'] })
  const joinContext = await browser.newContext({ permissions: ['microphone', 'camera'] })
  const hostPage = await hostContext.newPage()
  const joinPage = await joinContext.newPage()

  const inviteCode = await setupHost(hostPage)
  await setupJoiner(joinPage, inviteCode)

  await startCall(hostPage)
  await joinCall(joinPage)

  await waitForRemoteAudio(hostPage)
  await waitForRemoteAudio(joinPage)

  await hostContext.close()
  await joinContext.close()
})

test('video call delivers remote media', async ({ browser }) => {
  const hostContext = await browser.newContext({ permissions: ['microphone', 'camera'] })
  const joinContext = await browser.newContext({ permissions: ['microphone', 'camera'] })
  const hostPage = await hostContext.newPage()
  const joinPage = await joinContext.newPage()

  const inviteCode = await setupHost(hostPage)
  await setupJoiner(joinPage, inviteCode)

  await startCall(hostPage)
  await joinCall(joinPage)

  await hostPage.getByTestId('call-cam').click()
  await joinPage.getByTestId('call-cam').click()

  await expect(hostPage.getByTestId('call-local-video')).toBeVisible()
  await expect(joinPage.getByTestId('call-local-video')).toBeVisible()

  await waitForRemoteVideo(hostPage)
  await waitForRemoteVideo(joinPage)

  await hostContext.close()
  await joinContext.close()
})
