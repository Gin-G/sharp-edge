<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { sendChat } from '$lib/api';
  import { claudeKey } from '$lib/claudeKey';
  import type { ChatMessage } from '$lib/types';

  const MODELS = [
    { id: 'claude-sonnet-5', label: 'Sonnet 5' },
    { id: 'claude-opus-4-8', label: 'Opus 4.8' },
    { id: 'claude-haiku-4-5', label: 'Haiku 4.5' },
  ] as const;

  let messages: ChatMessage[] = [];
  let input = '';
  let model = MODELS[0].id;
  $: connected = !!$claudeKey;
  let thinking = false;
  let error = '';
  let msgList: HTMLElement;

  const WELCOME: ChatMessage = {
    role: 'assistant',
    content: "Hey — I'm Sharp Edge, your betting analyst. Ask me about your history, a potential bet, or what's working and what's not.",
  };

  onMount(() => {
    messages = [WELCOME];
  });

  async function send() {
    const text = input.trim();
    if (!text || thinking) return;
    if (!$claudeKey) {
      error = 'Connect your Anthropic API key in Settings to use chat.';
      return;
    }
    input = '';
    error = '';

    messages = [...messages, { role: 'user', content: text }];
    await tick();
    scrollToBottom();

    thinking = true;
    try {
      // Send all messages except the static welcome
      const history = messages.filter((m) => m !== WELCOME);
      const result = await sendChat(history, $claudeKey, model);
      // Replace messages with server-returned history (includes tool call turns)
      messages = [WELCOME, ...result.messages];
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      thinking = false;
      await tick();
      scrollToBottom();
    }
  }

  function scrollToBottom() {
    if (msgList) msgList.scrollTop = msgList.scrollHeight;
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function extractText(content: ChatMessage['content']): string {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content
        .filter((b): b is { type: 'text'; text: string } => typeof b === 'object' && (b as {type:string}).type === 'text')
        .map((b) => b.text)
        .join('');
    }
    return '';
  }

  function clearChat() {
    messages = [WELCOME];
    error = '';
  }
</script>

<svelte:head><title>Chat — Sharp Edge</title></svelte:head>

<div class="flex flex-col h-[calc(100vh-3rem)]">
  <!-- Header -->
  <div class="flex items-center justify-between mb-4 flex-shrink-0">
    <div>
      <h1 class="text-xl font-bold text-white">Chat</h1>
      <p class="text-sm text-slate-400 mt-0.5">Claude-powered betting analyst</p>
    </div>
    <div class="flex items-center gap-3">
      <select
        bind:value={model}
        class="input w-auto text-xs py-1.5"
      >
        {#each MODELS as m}
          <option value={m.id}>{m.label}</option>
        {/each}
      </select>
      <button class="btn-ghost text-xs" on:click={clearChat}>Clear</button>
    </div>
  </div>

  {#if !connected}
    <div class="card border-amber-800 bg-amber-950/30 text-amber-200 text-sm mb-4 flex-shrink-0">
      Chat runs on your own Anthropic account.
      <a href="/settings" class="underline font-medium">Connect your API key in Settings</a>
      to start — it stays in this browser and is never stored on the server.
    </div>
  {/if}

  {#if error}
    <div class="card border-red-800 bg-red-950/30 text-red-300 text-sm mb-4 flex-shrink-0">
      {error}
    </div>
  {/if}

  <!-- Messages -->
  <div
    class="flex-1 overflow-y-auto space-y-4 mb-4 pr-1 min-h-0"
    bind:this={msgList}
  >
    {#each messages as msg}
      {#if msg.role === 'user'}
        <div class="flex justify-end">
          <div class="max-w-[75%] bg-indigo-600 text-white rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm leading-relaxed">
            {extractText(msg.content)}
          </div>
        </div>
      {:else}
        <!-- Skip pure tool-result turns -->
        {#if extractText(msg.content)}
          <div class="flex justify-start gap-2.5">
            <div class="w-7 h-7 bg-indigo-800 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5">
              <svg class="w-4 h-4 text-indigo-300" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"/>
              </svg>
            </div>
            <div class="max-w-[80%] bg-surface-700 border border-border rounded-2xl rounded-tl-sm px-4 py-2.5 text-sm text-slate-200 leading-relaxed whitespace-pre-wrap">
              {extractText(msg.content)}
            </div>
          </div>
        {/if}
      {/if}
    {/each}

    {#if thinking}
      <div class="flex justify-start gap-2.5">
        <div class="w-7 h-7 bg-indigo-800 rounded-full flex items-center justify-center flex-shrink-0">
          <svg class="w-4 h-4 text-indigo-300 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
        </div>
        <div class="bg-surface-700 border border-border rounded-2xl rounded-tl-sm px-4 py-2.5">
          <div class="flex gap-1 items-center h-5">
            <span class="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
            <span class="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
            <span class="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
          </div>
        </div>
      </div>
    {/if}
  </div>

  <!-- Input -->
  <div class="flex-shrink-0 flex gap-3 items-end">
    <div class="flex-1 relative">
      <textarea
        bind:value={input}
        on:keydown={onKeydown}
        placeholder={connected ? 'Ask about your bets, a potential wager, league breakdown…' : 'Connect your Anthropic key in Settings to chat'}
        rows="2"
        class="input resize-none pr-12 py-3 leading-relaxed"
        disabled={thinking || !connected}
      ></textarea>
      <div class="absolute right-3 bottom-3 text-xs text-slate-600">⏎ send</div>
    </div>
    <button
      class="btn-primary flex-shrink-0 h-[52px] px-5"
      on:click={send}
      disabled={thinking || !input.trim() || !connected}
    >
      {#if thinking}
        <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
        </svg>
      {:else}
        <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5"/>
        </svg>
        Send
      {/if}
    </button>
  </div>
</div>
