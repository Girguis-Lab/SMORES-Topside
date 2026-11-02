True Reading Parameters component offset table:

| **Register Offset** | **Size (Registers)** | **Mode (R/W)** | **Data Type** | **Description**                                              |
| ------------------- | -------------------- | -------------- | ------------- | ------------------------------------------------------------ |
| 0                   | 2                    | R              | float         | The measured value from sensor                               |
| 2                   | 1                    | R              | uint16        | Parameter ID for this parameter. See: Appendix A.            |
| 3                   | 1                    | R/W            | uint16        | Units ID for this parameter. See: Appendix B.                |
| 4                   | 1                    | R              | uint16        | Data Quality ID: 0 = No errors or warnings 3 = Error reading parameter 5 = RDO Cap expired, 7 = No RDO cap detected. For additional errors or information, contact technical support. |
| 5                   | 2                    | R/W            | float         | Off line sentinel value: The value that’s returned on error or if the parameter isn’t available. The default sentinel is 0.0 |
