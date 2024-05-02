# Copyright (C) 2017 Amazon.com, Inc. or its affiliates
#
# Author: Ethan Faust <efaust@amazon.com>
# Author: Andrew Jorgensen <ajorgens@amazon.com>
#
# This file is part of cloud-init. See LICENSE file for license information.


class SimpleTable:
    """A minimal implementation of PrettyTable
    for distribution with cloud-init.
    """

    def __init__(self, fields):
        self.fields = fields
        self.rows = []

        # initialize list of 0s the same length
        # as the number of fields
        self.column_widths = [0] * len(self.fields)
        self.update_column_widths(fields)

    def update_column_widths(self, values):
        for i, value in enumerate(values):
            self.column_widths[i] = max(len(value), self.column_widths[i])

    def add_row(self, values):
        if len(values) > len(self.fields):
            raise TypeError("too many values")
        values = [str(value) for value in values]
        self.rows.append(values)
        self.update_column_widths(values)

    def _hdiv(self):
        """Returns a horizontal divider for the table."""
        return (
            "+" + "+".join(["-" * (w + 2) for w in self.column_widths]) + "+"
        )

    def _row(self, row):
        """Returns a formatted row."""
        return (
            "|"
            + "|".join(
                [
                    col.center(self.column_widths[i] + 2)
                    for i, col in enumerate(row)
                ]
            )
            + "|"
        )

class Example:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return f"Value: {self.value}"  # Added missing closing parenthesis

    def get_string(self):
        return "This is a string"  # Implemented get_string method
        return self.__str__()
