interface IconProps {
  size?: number
}

function Svg({ size = 18, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

export function Burst({ size = 40 }: IconProps) {
  const spokes = Array.from({ length: 12 }, (_, index) => index * 30)
  return (
    <svg
      className="burst"
      width={size}
      height={size}
      viewBox="-50 -50 100 100"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      {spokes.map((angle) => (
        <path key={angle} d="M0 -47 L5.2 -13 L0 1 L-5.2 -13 Z" transform={`rotate(${angle})`} />
      ))}
    </svg>
  )
}

export const PlusIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5v14M5 12h14" />
  </Svg>
)

export const ChatIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20 15a3 3 0 0 1-3 3H8l-4 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z" />
  </Svg>
)

export const ProjectsIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3" y="7" width="18" height="13" rx="2.5" />
    <path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7M3 12h18" />
  </Svg>
)

export const FolderIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="M3.5 6.5A1.5 1.5 0 0 1 5 5h4l2 2h8a1.5 1.5 0 0 1 1.5 1.5v9A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z" />
  </Svg>
)

export const DocumentsIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5M9 13h6M9 17h4" />
  </Svg>
)

export const SettingsIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5 6h14M5 12h14M5 18h14" />
    <circle cx="9" cy="6" r="2" fill="var(--bg-sidebar)" />
    <circle cx="15" cy="12" r="2" fill="var(--bg-sidebar)" />
    <circle cx="8" cy="18" r="2" fill="var(--bg-sidebar)" />
  </Svg>
)

export const SearchIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m16 16 4 4" />
  </Svg>
)

export const PanelIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3" y="4" width="18" height="16" rx="2.5" />
    <path d="M10 4v16" />
  </Svg>
)

export const SmilePlusIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M22 11v1a10 10 0 1 1-9-10" />
    <path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01M16 5h6M19 2v6" />
  </Svg>
)

export const HeartIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z" />
  </Svg>
)

export const GiftIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20 12v10H4V12M2 7h20v5H2zM12 22V7" />
    <path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7zM12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z" />
  </Svg>
)

export const BookIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20M4 19.5A2.5 2.5 0 0 0 6.5 22H20V2H6.5A2.5 2.5 0 0 0 4 4.5z" />
  </Svg>
)

export const MusicIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 18V5l12-2v13" />
    <circle cx="6" cy="18" r="3" />
    <circle cx="18" cy="16" r="3" />
  </Svg>
)

export const FlaskIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10 2v6L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 8V2M8.5 2h7M7 15h10" />
  </Svg>
)

export const PawIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="6.5" cy="9.5" r="1.7" />
    <circle cx="12" cy="7.5" r="1.7" />
    <circle cx="17.5" cy="9.5" r="1.7" />
    <path d="M12 12c-2.8 0-5 2.3-5 4.6 0 1.9 1.6 3 3.2 2.6 1-.2 2.6-.2 3.6 0 1.6.4 3.2-.7 3.2-2.6 0-2.3-2.2-4.6-5-4.6z" />
  </Svg>
)

export const GraduationIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M22 9 12 4 2 9l10 5 10-5zM6 11.5V16c0 1.7 2.7 3 6 3s6-1.3 6-3v-4.5" />
  </Svg>
)

export const DollarIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 2v20M17 5.5H9.5a3.25 3.25 0 0 0 0 6.5h5a3.25 3.25 0 0 1 0 6.5H6.5" />
  </Svg>
)

export const WrenchIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
  </Svg>
)

export const PlaneIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z" />
  </Svg>
)

export const BracesIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 3H7a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1M16 3h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-1" />
  </Svg>
)

export const BuildingIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 21V5.5A1.5 1.5 0 0 1 5.5 4h7A1.5 1.5 0 0 1 14 5.5V21M14 9h4.5A1.5 1.5 0 0 1 20 10.5V21M2.5 21h19" />
    <path d="M7.5 8h3M7.5 12h3M7.5 16h3M17 13h.01M17 17h.01" />
  </Svg>
)

export const PanelRightIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3" y="4" width="18" height="16" rx="2.5" />
    <path d="M14 4v16" />
  </Svg>
)

export const FilterIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="M7 6v12M17 6v12" />
    <circle cx="7" cy="10" r="2" fill="var(--bg-sidebar)" />
    <circle cx="17" cy="15" r="2" fill="var(--bg-sidebar)" />
  </Svg>
)

export const DownloadIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 16}>
    <path d="M12 4v11m0 0 4-4m-4 4-4-4M5 19h14" />
  </Svg>
)

export const ChevronIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 14}>
    <path d="m6 9 6 6 6-6" />
  </Svg>
)

export const CheckIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="m5 12.5 4.5 4.5L19 7" strokeWidth={2} />
  </Svg>
)

export const UserIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="8.5" r="3.5" />
    <path d="M5 19.5c1.2-3 3.8-4.5 7-4.5s5.8 1.5 7 4.5" />
  </Svg>
)

export const LibraryIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5 4v16M9.5 4v16M14 5.5l4.5 15" />
  </Svg>
)

export const ChartIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5 19V11M12 19V5M19 19v-5" />
  </Svg>
)

export const HelpIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.4 9.2a2.7 2.7 0 0 1 5.2 1c0 1.8-2.6 2.2-2.6 3.6" />
    <path d="M12 17.2h.01" />
  </Svg>
)

export const InfoIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 7.8h.01" />
  </Svg>
)

export const PlansIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 6.5l1.5 1.5L8 5.5M4 12.5l1.5 1.5L8 11.5M4 18.5l1.5 1.5L8 17.5" />
    <path d="M11.5 7h8.5M11.5 13h8.5M11.5 19h8.5" />
  </Svg>
)

export const LogoutIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14 4h-7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h7" />
    <path d="M10 12h10M17 8.5l3.5 3.5-3.5 3.5" />
  </Svg>
)

export const GlobeIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3a13.5 13.5 0 0 1 0 18M12 3a13.5 13.5 0 0 0 0 18" />
  </Svg>
)

export const ChevronUpDownIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="m8 9 4-4 4 4M8 15l4 4 4-4" />
  </Svg>
)

export const SendIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 17}>
    <path d="M12 19V5m0 0-6 6m6-6 6 6" strokeWidth={2} />
  </Svg>
)

export const CodeIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="m9 8-4 4 4 4M15 8l4 4-4 4" />
  </Svg>
)

export const PencilIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17z" />
  </Svg>
)

export const BriefcaseIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <rect x="3" y="7" width="18" height="13" rx="2.5" />
    <path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7" />
  </Svg>
)

export const BulbIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="M9.5 17h5M10 20h4M12 3a5.5 5.5 0 0 1 3.2 10c-.5.4-.7 1-.7 1.6h-5c0-.6-.2-1.2-.7-1.6A5.5 5.5 0 0 1 12 3" />
  </Svg>
)

export const LockIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 13}>
    <rect x="5" y="10" width="14" height="10" rx="2.2" />
    <path d="M8.5 10V7.5a3.5 3.5 0 0 1 7 0V10" />
  </Svg>
)

export const CloseIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 17}>
    <path d="m6 6 12 12M18 6 6 18" />
  </Svg>
)

export const UploadIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 16}>
    <path d="M12 16V4m0 0-4 4m4-4 4 4M5 19h14" />
  </Svg>
)

/**
 * A grid with one tile lifted out of place, for the projects empty state.
 * The reference art has a hand grabbing that tile; a hand at 44px rendered as
 * an unreadable smudge, so the offset tile carries the idea on its own.
 */
export const StackIcon = ({ size = 44 }: IconProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.4}
    strokeLinejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <rect x="2.5" y="2.5" width="8.5" height="8.5" />
    <rect x="13" y="2.5" width="8.5" height="8.5" />
    <rect x="2.5" y="13" width="8.5" height="8.5" />
    <rect x="12" y="12" width="9.5" height="9.5" />
  </svg>
)

export const GoogleIcon = ({ size = 18 }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 48 48" aria-hidden="true" focusable="false">
    <path
      fill="#4285F4"
      d="M45.12 24.5c0-1.56-.14-3.06-.4-4.5H24v8.51h11.84c-.51 2.75-2.06 5.08-4.39 6.64v5.52h7.11c4.16-3.83 6.56-9.47 6.56-16.17z"
    />
    <path
      fill="#34A853"
      d="M24 46c5.94 0 10.92-1.97 14.56-5.33l-7.11-5.52c-1.97 1.32-4.49 2.11-7.45 2.11-5.71 0-10.55-3.85-12.28-9.03H4.34v5.7C7.96 41.07 15.4 46 24 46z"
    />
    <path
      fill="#FBBC05"
      d="M11.72 28.23c-.44-1.32-.69-2.73-.69-4.18s.25-2.86.69-4.18v-5.7H4.34a21.9 21.9 0 0 0 0 19.76z"
    />
    <path
      fill="#EA4335"
      d="M24 10.75c3.23 0 6.13 1.11 8.41 3.29l6.31-6.31C34.91 4.18 29.93 2 24 2 15.4 2 7.96 6.93 4.34 14.17l7.38 5.7c1.73-5.18 6.57-9.12 12.28-9.12z"
    />
  </svg>
)

export const AppleIcon = ({ size = 20 }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
    <path d="M16.7 12.9c0-2.1 1.7-3.1 1.8-3.2-1-1.4-2.5-1.6-3-1.6-1.3-.1-2.5.7-3.2.7-.6 0-1.7-.7-2.8-.7-1.4 0-2.7.8-3.5 2.1-1.5 2.6-.4 6.4 1.1 8.5.7 1 1.5 2.2 2.6 2.1 1-.1 1.4-.7 2.7-.7s1.6.7 2.8.7c1.1 0 1.9-1 2.6-2 .8-1.2 1.1-2.3 1.2-2.4-.1-.1-2.3-.9-2.3-3.5zM14.6 6.7c.6-.7 1-1.7.9-2.7-.9 0-1.9.6-2.5 1.3-.5.6-1 1.6-.9 2.6 1 .1 1.9-.5 2.5-1.2z" />
  </svg>
)

export const FacebookIcon = ({ size = 20 }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <circle cx="12" cy="12" r="10" fill="#1877F2" />
    <path
      fill="#fff"
      d="M15.6 15.1l.44-2.89h-2.77v-1.87c0-.79.39-1.56 1.63-1.56h1.26V6.32s-1.14-.2-2.24-.2c-2.28 0-3.77 1.38-3.77 3.89v2.2H7.6v2.89h2.55V22a10 10 0 0 0 3.12 0v-6.9h2.33z"
    />
  </svg>
)

export const MailIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3" y="5" width="18" height="14" rx="2.5" />
    <path d="m3.5 7 8.5 6 8.5-6" />
  </Svg>
)

export const PhoneIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="7" y="2.5" width="10" height="19" rx="2.5" />
    <path d="M11 18.5h2" />
  </Svg>
)

export const MoreIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 16}>
    <circle cx="12" cy="5" r="1.4" fill="currentColor" />
    <circle cx="12" cy="12" r="1.4" fill="currentColor" />
    <circle cx="12" cy="19" r="1.4" fill="currentColor" />
  </Svg>
)

export const SpinnerIcon = (p: IconProps) => (
  <svg
    className="spinner"
    width={p.size ?? 15}
    height={p.size ?? 15}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    aria-hidden="true"
    focusable="false"
  >
    <path d="M12 3a9 9 0 1 0 9 9" />
  </svg>
)

export const AlertIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 8v5M12 16.2v.01" />
  </Svg>
)

export const ThumbUpIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M7 10v11M7 11l3.8-7.6A1.8 1.8 0 0 1 14 4.2V9h4.6a2 2 0 0 1 2 2.4l-1.4 7a2 2 0 0 1-2 1.6H4.5A1.5 1.5 0 0 1 3 18.5v-7A1.5 1.5 0 0 1 4.5 10z" />
  </Svg>
)

export const ThumbDownIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M17 14V3M17 13l-3.8 7.6A1.8 1.8 0 0 1 10 19.8V15H5.4a2 2 0 0 1-2-2.4l1.4-7a2 2 0 0 1 2-1.6h12.7A1.5 1.5 0 0 1 21 5.5v7a1.5 1.5 0 0 1-1.5 1.5z" />
  </Svg>
)

export const CopyIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="9" y="9" width="12" height="12" rx="2.5" />
    <path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5" />
  </Svg>
)

export const RetryIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 3v6h-6" />
  </Svg>
)

export const StopIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" stroke="none" />
  </Svg>
)

export const BranchIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="6.5" cy="5" r="2.2" />
    <circle cx="6.5" cy="19" r="2.2" />
    <circle cx="17.5" cy="8" r="2.2" />
    <path d="M6.5 7.2v9.6M6.5 12h5a6 6 0 0 0 6-1.9" />
  </Svg>
)

export const FlagIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5.5 21V4M5.5 5h10.8a.7.7 0 0 1 .55 1.14L14.5 9l2.35 2.86a.7.7 0 0 1-.55 1.14H5.5" />
  </Svg>
)

export const ShareIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 15V3M12 3 8 7M12 3l4 4" />
    <path d="M4 13v5.5A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V13" />
  </Svg>
)

export const LinkIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10 13.5a4 4 0 0 0 5.7.3l2.8-2.8a4 4 0 0 0-5.7-5.7l-1.6 1.6" />
    <path d="M14 10.5a4 4 0 0 0-5.7-.3l-2.8 2.8a4 4 0 0 0 5.7 5.7l1.6-1.6" />
  </Svg>
)

export const TrashIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 15}>
    <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6" />
  </Svg>
)
