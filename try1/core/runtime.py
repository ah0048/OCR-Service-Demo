"""Shared CPU, NVIDIA CUDA, and native macOS Metal device selection."""


def resolve_device(device='cpu'):
    if device not in ('cpu', 'cuda', 'mps', 'auto'):
        raise ValueError('Device must be cpu, cuda, mps or auto')
    if device == 'cpu':
        return 'cpu'
    import torch
    cuda_available = torch.cuda.is_available()
    mps_available = bool(getattr(torch.backends, 'mps', None) and
                         torch.backends.mps.is_available())
    if device == 'cuda' and not cuda_available:
        raise RuntimeError(
            'CUDA is unavailable. Use the RTX computer with an NVIDIA driver and '
            'CUDA-enabled PyTorch, or select --device cpu. See CUDA_README.md.')
    if device == 'mps' and not mps_available:
        raise RuntimeError(
            'Apple Metal (MPS) is unavailable. MPS requires native macOS with '
            'Apple silicon or a supported AMD GPU; Docker Desktop cannot expose MPS.')
    if device == 'auto':
        return 'cuda' if cuda_available else ('mps' if mps_available else 'cpu')
    return device
