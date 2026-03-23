from typing import List, Generator

from models.schemas import Block, DocumentNode


def build_document_tree(blocks: List[Block]) -> List[DocumentNode]:
    """
    Builds a hierarchical tree of DocumentNodes from a flat list of Blocks
    based on their heading_path properties.
    """
    roots = []
    node_map = {}

    def get_or_create_node(path: List[str]) -> DocumentNode:
        path_tuple = tuple(path)
        if path_tuple in node_map:
            return node_map[path_tuple]
            
        heading = path[-1] if path else "Unclassified"
        level = len(path)
        node = DocumentNode(heading=heading, heading_path=list(path), level=level)
        node_map[path_tuple] = node
        
        if level <= 1:
            roots.append(node)
        else:
            parent_path = path[:-1]
            parent_node = get_or_create_node(parent_path)
            parent_node.children.append(node)
            
        return node

    for block in blocks:
        # If a block has no heading, we place it under a generic root.
        path = block.heading_path if block.heading_path else ["Unclassified"]
        node = get_or_create_node(path)
        node.blocks.append(block)
        
    return roots


def iter_nodes_with_blocks(nodes: List[DocumentNode]) -> Generator[DocumentNode, None, None]:
    """
    Recursively yields any DocumentNode that contains at least one block directly under it.
    """
    for node in nodes:
        if node.blocks:
            yield node
        yield from iter_nodes_with_blocks(node.children)
