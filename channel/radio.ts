#!/usr/bin/env bun
// Radio: channel between a Claude Code session and the bot. One runs per session:
// RADIO_SESSION is the name the session introduces itself to the bot with (e.g. "alpha").
// Claude Code starts it as an MCP server over stdio; it connects to the bot over HTTP
// (incoming events over SSE, replies and permissions over POST). The bot never has to
// reach the adapter: every connection starts from here.
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import { readFile, stat } from 'node:fs/promises'
import { basename, extname, resolve } from 'node:path'

const SESSION = process.env.RADIO_SESSION ?? 'radio'
const BOT = process.env.RADIO_BOT_URL ?? 'http://127.0.0.1:8790'
const log = (...parts: unknown[]) => console.error(`[radio:${SESSION}]`, ...parts)

const mcp = new Server(
  { name: 'radio', version: '0.1.0' },
  {
    capabilities: {
      experimental: {
        'claude/channel': {},             // registers the event listener
        'claude/channel/permission': {},  // permission requests arrive here too
      },
      tools: {},
    },
    instructions:
      `Messages tagged <channel source="radio" session="${SESSION}"> come from a human talking ` +
      `over a voice radio. They are transcribed by speech recognition, so expect small errors, ` +
      `and the human cannot see your terminal: text you print there is never heard. ` +
      `You MUST answer every channel message by calling the "reply" tool, even for a one-line answer. ` +
      `Write for the ear: ` +
      `two or three short sentences, no code, no file paths, no markdown, no lists. ` +
      `Always reply in English over the radio, whatever language the project files or instructions use. ` +
      `Say what you did, what you found, or what you need, nothing more. Before implementing ` +
      `anything that is not trivial, send the plan in three sentences and wait for an ok. ` +
      `Set kind to "done" when a task is finished, "blocked" when you cannot go on without the human, ` +
      `"progress" for an unsolicited update, "answer" (default) when answering a question. ` +
      `Feedback protocol, because the human only hears you: when a message needs more than one tool call, ` +
      `your FIRST action is reply with kind "progress", one sentence with what you understood and what you ` +
      `will do. While you work, reply with kind "progress" at each milestone (cause found, edits done, tests ` +
      `run), one sentence each, at most one per minute. Finish with kind "done" or "blocked": a turn that ` +
      `ends without a reply is read out to the human as a failure. ` +
      `Board: the human cannot see this terminal, but they can read a board page on their phone. ` +
      `To let them see something, use the "show" tool: a draft you will publish in their name (merge ` +
      `request or ticket description, commit message, review comment), a code snippet, a diff, or ` +
      `screenshots (save a PNG first, then pass its path). Never read code, diffs or long text aloud. ` +
      `After show, say in one sentence with reply what you put on the board and what you need. Before ` +
      `you publish text in their name, show the draft and wait for their OK over the radio. The board is the ` +
      `only place for their code, diffs, drafts and screenshots: never Claude artifacts, gists or other ` +
      `online services. For frontend work, $RADIO_DIR/tools/overview.py prepares ` +
      `Storybook links on the tailnet and phone and desktop screenshots for the board; never start ` +
      `Storybook with pnpm yourself and never give localhost links. ` +
      `In your first reply of each task set topic: two or three words on what you work on (for example ` +
      `"media viewer zoom"), no ticket numbers. The human runs several sessions at once and tells them apart by it. ` +
      `Your session name is "${SESSION}": that is how the human addresses you.`,
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'reply',
    description: 'Speak a short message to the human over the radio. Use it first to acknowledge a task (kind progress), then at each milestone, then to finish (kind done or blocked).',
    inputSchema: {
      type: 'object',
      properties: {
        text: { type: 'string', description: 'Two or three short sentences, written to be heard, not read' },
        kind: {
          type: 'string',
          enum: ['answer', 'progress', 'done', 'blocked'],
          description: 'answer: reply to a question (default). progress: unsolicited update. done: task finished. blocked: you need the human to go on.',
        },
        topic: {
          type: 'string',
          description: 'Two or three words on what you are working on, e.g. "media viewer zoom", no ticket numbers. Set it in your first reply of each task and when the task changes: the human tells sessions apart by it and can call you by one of its words.',
        },
      },
      required: ['text'],
    },
  }, {
    name: 'show',
    description: 'Put something on the human\'s board, a page they read on their phone: a draft to approve ' +
      '(merge request or ticket description, commit message, comment), a code snippet, a diff, or screenshots ' +
      'and other files. Use it whenever they ask to see something and before you publish text in their name. ' +
      'After showing, tell them in one sentence with reply. They answer over the radio or by typing on the board: typed text reaches you like a spoken message.',
    inputSchema: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'Short title, e.g. "Merge request description" or "Button after the fix"' },
        text: { type: 'string', description: 'The content to read: plain text or markdown, a code snippet, or a unified diff' },
        format: {
          type: 'string',
          enum: ['text', 'code', 'diff'],
          description: 'How to display text. text (default): wrapped, for prose and markdown. code: monospace, not wrapped. diff: unified diff with added and removed lines colored',
        },
        files: {
          type: 'array',
          items: { type: 'string' },
          description: 'Paths of files to attach, relative to the session directory or absolute. png, jpg, gif and webp are shown as pictures, wav, mp3, ogg and m4a as audio players, anything else as text (.diff and .patch colored as diffs)',
        },
      },
      required: ['title'],
    },
  }, {
    name: 'move',
    description: 'Move this radio session into another folder: Claude Code restarts there with the radio, and your brief ' +
      'becomes the first message of the new session (this conversation does not come along). Use it from the radio lobby ' +
      'once you know which project the human means. Say in one sentence with reply where you are going before calling it.',
    inputSchema: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Absolute path of the project folder: a repository, not a folder that only contains repositories' },
        brief: { type: 'string', description: 'What the human asked, in their words, plus anything the next session needs to know' },
      },
      required: ['path', 'brief'],
    },
  }],
}))

// Files to put on the board: images as images, the rest as text. The limits are the same
// as the bot's (bot/board.py); here they give the agent a clear error right away
const IMAGE_TYPES: Record<string, string> = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.m4a': 'audio/mp4',
}
const MAX_IMAGE = 15 * 1024 * 1024
const MAX_TEXT_FILE = 512 * 1024

async function attach(path: string) {
  const full = resolve(process.cwd(), path)
  const info = await stat(full).catch(() => null)
  if (!info) throw new Error(`${path} does not exist`)
  if (!info.isFile()) throw new Error(`${path} is not a file`)
  const mime = IMAGE_TYPES[extname(full).toLowerCase()] ?? 'text/plain'
  const limit = mime === 'text/plain' ? MAX_TEXT_FILE : MAX_IMAGE
  if (info.size > limit) throw new Error(`${path} is ${Math.ceil(info.size / 1024)} KB, the limit is ${limit / 1024} KB`)
  return { name: basename(full), mime, data: (await readFile(full)).toString('base64') }
}

async function show(args: { title?: string; text?: string; format?: string; files?: string[] }) {
  const files = await Promise.all((args.files ?? []).map(attach))
  await post('/show', { session: SESSION, title: args.title ?? '', text: args.text ?? '', format: args.format ?? 'text', files })
  return `On the board as "${args.title}". Now say in one sentence with reply what you put there and what you ` +
    `need from them; do not read it aloud. If you need their OK, wait for it: they answer over the radio.`
}

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  if (req.params.name === 'move') {
    const { path, brief } = req.params.arguments as { path: string; brief: string }
    try {
      await post('/move', { session: SESSION, dir: path, brief })
      return { content: [{ type: 'text', text: `Moving: in a few seconds this session closes and reopens in ${path}. Do nothing else now.` }] }
    } catch (err) {
      return { content: [{ type: 'text', text: `Not moved: ${(err as Error).message}` }], isError: true }
    }
  }
  if (req.params.name === 'show') {
    try {
      const text = await show(req.params.arguments as Parameters<typeof show>[0])
      return { content: [{ type: 'text', text }] }
    } catch (err) {
      return { content: [{ type: 'text', text: `Not shown: ${(err as Error).message}` }], isError: true }
    }
  }
  if (req.params.name !== 'reply') throw new Error(`unknown tool: ${req.params.name}`)
  const { text, kind, topic } = req.params.arguments as { text: string; kind?: string; topic?: string }
  await post('/say', { session: SESSION, text, kind: kind ?? 'answer', topic })
  return { content: [{ type: 'text', text: 'sent' }] }
})

// Claude Code (the app, not the model) sends this notification when a permission dialog opens
const PermissionRequestSchema = z.object({
  method: z.literal('notifications/claude/channel/permission_request'),
  params: z.object({
    request_id: z.string(),
    tool_name: z.string(),
    description: z.string(),
    input_preview: z.string(),
  }),
})
mcp.setNotificationHandler(PermissionRequestSchema, async ({ params }) => {
  await post('/permission', { session: SESSION, ...params })
})

async function post(path: string, body: unknown) {
  const res = await fetch(BOT + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().then((d: { error?: string }) => d.error ?? '', () => '')
    throw new Error(`bot ${path}: HTTP ${res.status}${detail ? `, ${detail}` : ''}`)
  }
}

const transport = new StdioServerTransport()
// When Claude Code closes (or dies), stdio closes: exit right away, otherwise the SSE
// stream to the bot would stay open and the session would still look connected
transport.onclose = () => { log('Claude Code closed the channel, exiting'); process.exit(0) }
process.stdin.on('end', () => process.exit(0))
await mcp.connect(transport)
log('connected to Claude Code, looking for the bot at', BOT)

// Events from the bot: one SSE stream per session. If it drops, retry every 3 seconds.
async function listen() {
  for (;;) {
    try {
      const res = await fetch(`${BOT}/events?session=${encodeURIComponent(SESSION)}&dir=${encodeURIComponent(process.cwd())}`)
      if (!res.ok || !res.body) throw new Error(`bot /events: HTTP ${res.status}`)
      log('registered with the bot')
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let pending = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        pending += decoder.decode(value, { stream: true })
        let cut: number
        while ((cut = pending.indexOf('\n\n')) >= 0) {
          const block = pending.slice(0, cut)
          pending = pending.slice(cut + 2)
          await handleBlock(block)
        }
      }
      log('the bot closed the connection')
    } catch (err) {
      log('bot unreachable:', (err as Error).message)
    }
    await Bun.sleep(3000)
  }
}

async function handleBlock(block: string) {
  const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n')
  if (!data) return  // comment or keepalive
  const event = JSON.parse(data)
  if (event.type === 'message') {
    const text = String(event.text)
    // Reminder appended to longer messages: the server instructions get lost in long sessions
    const hint = text.split(/\s+/).length >= 6
      ? '\n\n[radio: if this takes more than one tool call, first reply with kind progress in one sentence, then a progress reply at each milestone, then kind done or blocked. Code, diffs, drafts and screenshots go on the board with show, never read aloud]'
      : ''
    await mcp.notification({
      method: 'notifications/claude/channel',
      params: {
        content: text + hint,
        meta: { session: SESSION, user: String(event.user ?? ''), reply_tool: 'reply', spoken: 'yes' },
      },
    })
  } else if (event.type === 'permission') {
    await mcp.notification({
      method: 'notifications/claude/channel/permission',
      params: { request_id: String(event.request_id), behavior: event.behavior === 'allow' ? 'allow' : 'deny' },
    })
  }
}

listen()
