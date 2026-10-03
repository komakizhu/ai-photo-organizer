import { fireEvent, render, waitFor } from '@testing-library/svelte';
import { assetFactory } from '@test-data/factories/asset-factory';
import AdaptiveImage from './AdaptiveImage.svelte';

describe('AdaptiveImage display bounds', () => {
  it('uses loaded image dimensions when metadata has the wrong aspect ratio', async () => {
    const asset = assetFactory.build({ width: 1000, height: 1000, thumbhash: null });
    const { container, getByTestId } = render(AdaptiveImage, {
      asset,
      container: { width: 1000, height: 1000 },
    });
    const image = getByTestId('thumbnail');
    Object.defineProperties(image, {
      naturalWidth: { value: 1200 },
      naturalHeight: { value: 900 },
    });
    await fireEvent.load(image);
    const bounds = container.querySelector('[data-photo-bounds]') as HTMLElement;
    await waitFor(() => expect(bounds.style.height).toBe('750px'));
    expect(bounds.style.top).toBe('125px');
    expect(bounds.style.width).toBe('1000px');
  });
});
