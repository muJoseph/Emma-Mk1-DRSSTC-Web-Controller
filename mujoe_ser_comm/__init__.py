"""
Serial communication module for STM32 DRSSTC driver communication
"""

from .crc import calculate_crc16_ccitt, calculate_crc16_ccitt_hex

__all__ = ['calculate_crc16_ccitt', 'calculate_crc16_ccitt_hex']
