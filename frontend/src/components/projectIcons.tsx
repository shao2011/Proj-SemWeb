import {
  BookIcon,
  BracesIcon,
  BriefcaseIcon,
  BulbIcon,
  ChatIcon,
  CodeIcon,
  DocumentsIcon,
  DollarIcon,
  FlaskIcon,
  FolderIcon,
  GiftIcon,
  GlobeIcon,
  GraduationIcon,
  HeartIcon,
  MusicIcon,
  PawIcon,
  PencilIcon,
  PlaneIcon,
  WrenchIcon,
} from './icons'

/**
 * The icon keys are what the backend stores; the set can grow freely, and an
 * unknown key (from a newer client, say) falls back to the folder.
 */
export const PROJECT_ICONS: { key: string; Icon: React.ComponentType<{ size?: number }> }[] = [
  { key: 'folder', Icon: FolderIcon },
  { key: 'heart', Icon: HeartIcon },
  { key: 'gift', Icon: GiftIcon },
  { key: 'braces', Icon: BracesIcon },
  { key: 'book', Icon: BookIcon },
  { key: 'graduation', Icon: GraduationIcon },
  { key: 'dollar', Icon: DollarIcon },
  { key: 'briefcase', Icon: BriefcaseIcon },
  { key: 'code', Icon: CodeIcon },
  { key: 'flask', Icon: FlaskIcon },
  { key: 'bulb', Icon: BulbIcon },
  { key: 'music', Icon: MusicIcon },
  { key: 'paw', Icon: PawIcon },
  { key: 'plane', Icon: PlaneIcon },
  { key: 'globe', Icon: GlobeIcon },
  { key: 'wrench', Icon: WrenchIcon },
  { key: 'pencil', Icon: PencilIcon },
  { key: 'chat', Icon: ChatIcon },
  { key: 'documents', Icon: DocumentsIcon },
]

export const PROJECT_COLORS = [
  '#d8d4ce',
  '#e5604a',
  '#e0824e',
  '#e2c04d',
  '#4fae63',
  '#4a83e0',
  '#8f6ae0',
  '#d873ac',
]

export function ProjectIcon({
  icon,
  color,
  size,
}: {
  icon: string | null | undefined
  color: string | null | undefined
  size?: number
}) {
  const entry = PROJECT_ICONS.find((item) => item.key === icon)
  const Icon = entry?.Icon ?? FolderIcon
  return (
    <span className="project-icon" style={color ? { color } : undefined}>
      <Icon size={size} />
    </span>
  )
}
