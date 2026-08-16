interface Props {
  title: string
  description?: string
}

export function SystemPendingPage({ title, description = '系统待接入' }: Props) {
  return (
    <div className="page-state" role="status">
      <h1>{title}</h1>
      <p className="empty-copy">{description}</p>
    </div>
  )
}
