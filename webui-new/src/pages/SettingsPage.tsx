import { PageHeader } from '@/components/layout/PageHeader'
import { EnvPreview } from '@/components/settings/EnvPreview'

export default function SettingsPage() {
  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="设置"
        description="查看当前生效的运行时配置（只读）"
      />
      <EnvPreview />
    </div>
  )
}
