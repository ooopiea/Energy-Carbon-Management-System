import { useState } from 'react'
import { MessageSquareText, Send } from 'lucide-react'
import { Card } from './Layout'
import { useAppStore } from '../stores/appStore'

export function NaturalLanguagePrompt({ placeholder, prompt }: { placeholder: string; prompt?: string }) {
  const [text, setText] = useState(prompt ?? '')
  const { sendChatMessage, chatLoading, setActivePage, setChatRole } = useAppStore()
  const send = async () => {
    if (!text.trim()) return
    setChatRole('engineer')
    await sendChatMessage(text)
    setText('')
    setActivePage('facility_chat')
  }
  return <Card title="自然语言工程师" icon={<MessageSquareText className="icon-sm industrial" />}>
    <div className="mini-chat"><textarea value={text} onChange={event => setText(event.target.value)} rows={3} placeholder={placeholder} /><button className="btn primary" disabled={!text.trim() || chatLoading} onClick={() => void send()}><Send />{chatLoading ? '分析中…' : '发送并查看事件'}</button><small>涉及运行变化时先生成事件草案，不会直接绕过审批。</small></div>
  </Card>
}
