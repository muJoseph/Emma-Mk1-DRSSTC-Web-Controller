"""
CRC-16 CCITT implementation in Python.
"""

def calculate_crc16_ccitt(data: bytes) -> int:
    """
    Calculate CRC-16 CCITT checksum for the given data.
    
    Args:
        data: Input data as bytes
        
    Returns:
        CRC-16 CCITT checksum as integer
    """
    # CRC-CCITT polynomial
    POLY = 0x1021
    
    # Initial CRC value
    INIT_CRC = 0xFFFF
    
    crc = INIT_CRC
    
    for byte in data:
        # XOR data byte with upper byte of CRC
        crc ^= (byte << 8)
        
        # Process each bit
        for _ in range(8):
            if crc & 0x8000:  # If MSB is 1
                # Shift left and XOR with polynomial
                crc = ((crc << 1) ^ POLY) & 0xFFFF
            else:
                # Shift left
                crc = (crc << 1) & 0xFFFF
    
    return crc


def calculate_crc16_ccitt_hex(data: bytes) -> str:
    """
    Calculate CRC-16 CCITT checksum and return as hexadecimal string.
    
    Args:
        data: Input data as bytes
        
    Returns:
        CRC-16 CCITT checksum as hexadecimal string (e.g., "0x1234")
    """
    crc = calculate_crc16_ccitt(data)
    return f"0x{crc:04X}"


# Example usage and test
if __name__ == "__main__":
    # Test with some sample data
    test_data = b"Hello, World!"
    crc = calculate_crc16_ccitt(test_data)
    crc_hex = calculate_crc16_ccitt_hex(test_data)
    
    print(f"Data: {test_data}")
    print(f"CRC-16 CCITT: {crc} ({crc_hex})")
    
    # Test with empty data
    empty_crc = calculate_crc16_ccitt(b"")
    print(f"Empty data CRC: {empty_crc} (0x{empty_crc:04X})")
