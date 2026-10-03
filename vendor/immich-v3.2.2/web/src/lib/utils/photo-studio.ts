export const isPhotoStudioRoute = (pathname: string) =>
  ['/photo-studio', '/organize', '/photo-groups', '/duplicates'].some(
    (route) => pathname === route || pathname.startsWith(`${route}/`),
  );
